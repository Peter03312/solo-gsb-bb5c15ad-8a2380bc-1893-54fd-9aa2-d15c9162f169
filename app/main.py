"""冷链运输温度事件核验服务 HTTP API。"""
import json
import math
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import Settings, get_settings
from .db import connect, init_db
from .intervals import compute_intervals, diff_intervals
from .models import (
    CorrectionIn,
    DisputeIn,
    DisputeOut,
    DisputesResponse,
    IntervalsResponse,
    ReadingIn,
    ReadingsResponse,
)
from .util import to_utc_iso, utc_now_iso


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时完成数据库初始化（建表，幂等）
    settings = get_settings()
    init_db(settings.db_path)
    yield


app = FastAPI(
    title="冷链运输温度事件核验服务",
    version="1.1.0",
    description=(
        "按设备提交 / 更正 / 撤销温度读数，自动核验并维护超温区间。"
        "连续两次有效读数超过上限即形成超温区间，区间以起点事件号标识。"
        "支持以争议单形式申请更正 / 撤销：申请时冻结读数及修订号，"
        "批准时核对修订号，已变化则转为过期，不再改动读数。"
    ),
    lifespan=lifespan,
)


def _json_safe(value):
    """递归清洗校验错误内容，保证任何非法输入都能序列化为清晰的 422 响应。"""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Exception):
        return str(value)
    return value


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # 默认处理器在遇到 inf 等非法值时会序列化失败，这里统一清洗后返回
    return JSONResponse(status_code=422, content={"detail": _json_safe(exc.errors())})


# ---------------------------------------------------------------- 公共辅助

def _load_valid_readings(conn, device_id: str) -> list[dict]:
    """读取设备全部有效读数，按 (采样时间, 事件号) 排序。"""
    rows = conn.execute(
        "SELECT event_no, sampled_at, temperature FROM readings "
        "WHERE device_id = ? AND revoked = 0 "
        "ORDER BY sampled_at, event_no",
        (device_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _current_intervals(conn, device_id: str, limit: float) -> list[dict]:
    return compute_intervals(_load_valid_readings(conn, device_id), limit)


def _mutate(settings: Settings, device_id: str, op: Callable) -> dict:
    """在单事务内执行写操作并重算区间，返回 当前区间 + 本次差异。

    BEGIN IMMEDIATE 保证同一时刻只有一个写事务，避免并发写导致区间不一致。
    """
    with connect(settings.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            before = _current_intervals(conn, device_id, settings.temp_limit)
            op(conn)
            after = _current_intervals(conn, device_id, settings.temp_limit)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {
        "device_id": device_id,
        "limit": settings.temp_limit,
        "intervals": after,
        "changes": diff_intervals(before, after),
    }


def _get_reading(conn, device_id: str, event_no: str):
    return conn.execute(
        "SELECT event_no, sampled_at, temperature, revoked, revision FROM readings "
        "WHERE device_id = ? AND event_no = ?",
        (device_id, event_no),
    ).fetchone()


# ---------------------------------------------------------------- 写接口

@app.post(
    "/devices/{device_id}/readings",
    response_model=IntervalsResponse,
    summary="提交一条温度读数（同号同内容幂等，同号异内容拒绝）",
)
def submit_reading(
    device_id: str,
    body: ReadingIn,
    settings: Settings = Depends(get_settings),
):
    sampled_at = to_utc_iso(body.sampled_at)

    def op(conn):
        existing = _get_reading(conn, device_id, body.event_no)
        if existing is not None:
            same = (
                existing["sampled_at"] == sampled_at
                and existing["temperature"] == body.temperature
            )
            if same:
                return  # 幂等重放：内容完全一致，不产生任何变化
            raise HTTPException(
                status_code=409,
                detail=(
                    f"event_no '{body.event_no}' already exists on device "
                    f"'{device_id}' with different content; "
                    f"use PUT /devices/{device_id}/readings/{body.event_no} to correct it"
                ),
            )
        now = utc_now_iso()
        conn.execute(
            "INSERT INTO readings "
            "(device_id, event_no, sampled_at, temperature, revoked, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?)",
            (device_id, body.event_no, sampled_at, body.temperature, now, now),
        )

    return _mutate(settings, device_id, op)


@app.put(
    "/devices/{device_id}/readings/{event_no}",
    response_model=IntervalsResponse,
    summary="按事件号更正读数的采样时间与温度",
)
def correct_reading(
    device_id: str,
    event_no: str,
    body: CorrectionIn,
    settings: Settings = Depends(get_settings),
):
    sampled_at = to_utc_iso(body.sampled_at)

    def op(conn):
        existing = _get_reading(conn, device_id, event_no)
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail=f"reading '{event_no}' not found on device '{device_id}'",
            )
        if existing["revoked"]:
            raise HTTPException(
                status_code=409,
                detail=f"reading '{event_no}' on device '{device_id}' has been revoked "
                f"and cannot be corrected",
            )
        conn.execute(
            "UPDATE readings SET sampled_at = ?, temperature = ?, "
            "revision = revision + 1, updated_at = ? "
            "WHERE device_id = ? AND event_no = ?",
            (sampled_at, body.temperature, utc_now_iso(), device_id, event_no),
        )

    return _mutate(settings, device_id, op)


@app.delete(
    "/devices/{device_id}/readings/{event_no}",
    response_model=IntervalsResponse,
    summary="按事件号撤销读数（墓碑式，重复撤销幂等）",
)
def revoke_reading(
    device_id: str,
    event_no: str,
    settings: Settings = Depends(get_settings),
):
    def op(conn):
        existing = _get_reading(conn, device_id, event_no)
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail=f"reading '{event_no}' not found on device '{device_id}'",
            )
        if existing["revoked"]:
            return  # 已撤销：幂等，不产生新的区间变化
        conn.execute(
            "UPDATE readings SET revoked = 1, revision = revision + 1, updated_at = ? "
            "WHERE device_id = ? AND event_no = ?",
            (utc_now_iso(), device_id, event_no),
        )

    return _mutate(settings, device_id, op)


# ---------------------------------------------------------------- 查询接口

@app.get(
    "/devices/{device_id}/intervals",
    response_model=IntervalsResponse,
    summary="查询设备当前全部超温区间",
)
def list_intervals(device_id: str, settings: Settings = Depends(get_settings)):
    with connect(settings.db_path) as conn:
        intervals = _current_intervals(conn, device_id, settings.temp_limit)
    return {
        "device_id": device_id,
        "limit": settings.temp_limit,
        "intervals": intervals,
        "changes": {"added": [], "changed": [], "invalidated": []},
    }


@app.get(
    "/devices/{device_id}/readings",
    response_model=ReadingsResponse,
    summary="查询设备全部读数（含已撤销），按采样时间、事件号排序",
)
def list_readings(device_id: str, settings: Settings = Depends(get_settings)):
    with connect(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT event_no, sampled_at, temperature, revoked FROM readings "
            "WHERE device_id = ? ORDER BY sampled_at, event_no",
            (device_id,),
        ).fetchall()
    return {
        "device_id": device_id,
        "readings": [
            {
                "event_no": r["event_no"],
                "sampled_at": r["sampled_at"],
                "temperature": r["temperature"],
                "revoked": bool(r["revoked"]),
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------- 争议

def _get_dispute(conn, device_id: str, dispute_no: str):
    return conn.execute(
        "SELECT * FROM disputes WHERE device_id = ? AND dispute_no = ?",
        (device_id, dispute_no),
    ).fetchone()


def _dispute_out(row) -> dict:
    return {
        "device_id": row["device_id"],
        "dispute_no": row["dispute_no"],
        "event_no": row["event_no"],
        "action": row["action"],
        "target": (
            {
                "sampled_at": row["target_sampled_at"],
                "temperature": row["target_temperature"],
            }
            if row["action"] == "correct"
            else None
        ),
        "reason": row["reason"],
        "status": row["status"],
        "frozen": {
            "sampled_at": row["frozen_sampled_at"],
            "temperature": row["frozen_temperature"],
            "revoked": bool(row["frozen_revoked"]),
            "revision": row["frozen_revision"],
        },
        "decision": row["decision"],
        "decided_at": row["decided_at"],
        "expire_reason": row["expire_reason"],
        "changes": json.loads(row["changes_json"]) if row["changes_json"] else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@app.post(
    "/devices/{device_id}/disputes",
    response_model=DisputeOut,
    status_code=201,
    summary="提出争议：申请更正或撤销一条现有读数（同号同内容幂等，异内容拒绝）",
)
def file_dispute(
    device_id: str,
    body: DisputeIn,
    response: Response,
    settings: Settings = Depends(get_settings),
):
    target_sampled_at = to_utc_iso(body.sampled_at) if body.sampled_at else None

    with connect(settings.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = _get_dispute(conn, device_id, body.dispute_no)
            if existing is not None:
                same = (
                    existing["event_no"] == body.event_no
                    and existing["action"] == body.action
                    and existing["target_sampled_at"] == target_sampled_at
                    and existing["target_temperature"] == body.temperature
                    and existing["reason"] == body.reason
                )
                if same:
                    # 幂等重放：返回原申请（即使目标读数此后已变化）
                    conn.rollback()
                    response.status_code = 200
                    return _dispute_out(existing)
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"dispute '{body.dispute_no}' already exists on device "
                        f"'{device_id}' with different content"
                    ),
                )
            reading = _get_reading(conn, device_id, body.event_no)
            if reading is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"reading '{body.event_no}' not found on device '{device_id}'",
                )
            if reading["revoked"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"reading '{body.event_no}' on device '{device_id}' has been "
                    f"revoked and cannot be disputed",
                )
            now = utc_now_iso()
            conn.execute(
                "INSERT INTO disputes "
                "(device_id, dispute_no, event_no, action, target_sampled_at, "
                " target_temperature, reason, status, frozen_sampled_at, "
                " frozen_temperature, frozen_revoked, frozen_revision, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
                (
                    device_id,
                    body.dispute_no,
                    body.event_no,
                    body.action,
                    target_sampled_at,
                    body.temperature,
                    body.reason,
                    reading["sampled_at"],
                    reading["temperature"],
                    reading["revoked"],
                    reading["revision"],
                    now,
                    now,
                ),
            )
            row = _get_dispute(conn, device_id, body.dispute_no)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return _dispute_out(row)


def _decide_dispute(
    settings: Settings, device_id: str, dispute_no: str, decision: str
) -> dict:
    """在单事务内批准 / 驳回争议。

    批准时核对冻结修订号：读数已被直接操作或其他争议改变（修订号不一致，
    即使现值恰好相同）→ 转为过期并保留原因，不再改动读数；仍匹配则按
    现有规则执行更正或撤销，并记录决定当次的区间差异。
    """
    with connect(settings.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            dispute = _get_dispute(conn, device_id, dispute_no)
            if dispute is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"dispute '{dispute_no}' not found on device '{device_id}'",
                )
            if dispute["status"] != "pending":
                # 争议只能进入一个终态：同一决定重试返回首次结果，反向决定冲突
                if dispute["decision"] == decision:
                    conn.rollback()
                    return _dispute_out(dispute)
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"dispute '{dispute_no}' on device '{device_id}' is already "
                        f"'{dispute['status']}' (decision: {dispute['decision']}); "
                        f"cannot apply decision '{decision}'"
                    ),
                )

            before = _current_intervals(conn, device_id, settings.temp_limit)
            now = utc_now_iso()
            expire_reason = None
            if decision == "approve":
                reading = _get_reading(conn, device_id, dispute["event_no"])
                if reading is None:
                    expire_reason = "target reading no longer exists"
                elif reading["revoked"]:
                    expire_reason = (
                        "target reading has been revoked since the dispute was filed"
                    )
                elif reading["revision"] != dispute["frozen_revision"]:
                    expire_reason = (
                        f"target reading changed since the dispute was filed "
                        f"(revision {dispute['frozen_revision']} -> "
                        f"{reading['revision']})"
                    )
                elif dispute["action"] == "correct":
                    conn.execute(
                        "UPDATE readings SET sampled_at = ?, temperature = ?, "
                        "revision = revision + 1, updated_at = ? "
                        "WHERE device_id = ? AND event_no = ?",
                        (
                            dispute["target_sampled_at"],
                            dispute["target_temperature"],
                            now,
                            device_id,
                            dispute["event_no"],
                        ),
                    )
                else:  # revoke
                    conn.execute(
                        "UPDATE readings SET revoked = 1, revision = revision + 1, "
                        "updated_at = ? WHERE device_id = ? AND event_no = ?",
                        (now, device_id, dispute["event_no"]),
                    )

            after = _current_intervals(conn, device_id, settings.temp_limit)
            changes = diff_intervals(before, after)
            status = (
                "expired"
                if expire_reason
                else ("approved" if decision == "approve" else "rejected")
            )
            conn.execute(
                "UPDATE disputes SET status = ?, decision = ?, decided_at = ?, "
                "expire_reason = ?, changes_json = ?, updated_at = ? "
                "WHERE device_id = ? AND dispute_no = ?",
                (
                    status,
                    decision,
                    now,
                    expire_reason,
                    json.dumps(changes),
                    now,
                    device_id,
                    dispute_no,
                ),
            )
            row = _get_dispute(conn, device_id, dispute_no)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return _dispute_out(row)


@app.post(
    "/devices/{device_id}/disputes/{dispute_no}/approve",
    response_model=DisputeOut,
    summary="批准争议：修订号仍匹配则执行更正 / 撤销，否则转为过期",
)
def approve_dispute(
    device_id: str,
    dispute_no: str,
    settings: Settings = Depends(get_settings),
):
    return _decide_dispute(settings, device_id, dispute_no, "approve")


@app.post(
    "/devices/{device_id}/disputes/{dispute_no}/reject",
    response_model=DisputeOut,
    summary="驳回争议：不改动读数",
)
def reject_dispute(
    device_id: str,
    dispute_no: str,
    settings: Settings = Depends(get_settings),
):
    return _decide_dispute(settings, device_id, dispute_no, "reject")


@app.get(
    "/devices/{device_id}/disputes/{dispute_no}",
    response_model=DisputeOut,
    summary="查询争议单：冻结依据、终态与执行结果",
)
def get_dispute(
    device_id: str,
    dispute_no: str,
    settings: Settings = Depends(get_settings),
):
    with connect(settings.db_path) as conn:
        row = _get_dispute(conn, device_id, dispute_no)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"dispute '{dispute_no}' not found on device '{device_id}'",
        )
    return _dispute_out(row)


@app.get(
    "/devices/{device_id}/disputes",
    response_model=DisputesResponse,
    summary="查询设备全部争议单（按创建时间、争议号排序）",
)
def list_disputes(device_id: str, settings: Settings = Depends(get_settings)):
    with connect(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM disputes WHERE device_id = ? "
            "ORDER BY created_at, dispute_no",
            (device_id,),
        ).fetchall()
    return {
        "device_id": device_id,
        "disputes": [_dispute_out(r) for r in rows],
    }


@app.get("/health", summary="健康检查")
def health():
    return {"status": "ok"}


@app.get("/", summary="服务信息")
def root(settings: Settings = Depends(get_settings)):
    return {
        "service": "coldchain-temperature-verification",
        "version": "1.1.0",
        "temp_limit": settings.temp_limit,
        "docs": "/docs",
    }
