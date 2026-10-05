"""争议（dispute）端到端测试：申请、幂等、批准 / 驳回 / 过期、终态与查询。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

DEVICE = "truck-001"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("TEMP_LIMIT", "8.0")
    with TestClient(app) as c:
        yield c


def post_reading(client, event_no, ts, temp, device=DEVICE):
    return client.post(
        f"/devices/{device}/readings",
        json={"event_no": event_no, "sampled_at": ts, "temperature": temp},
    )


def file(client, dispute_no="D1", event_no="E1", action="correct",
         ts="2026-10-01T08:00:00Z", temp=5.0, reason="sensor drift",
         device=DEVICE):
    body = {"dispute_no": dispute_no, "event_no": event_no,
            "action": action, "reason": reason}
    if ts is not None:
        body["sampled_at"] = ts
    if temp is not None:
        body["temperature"] = temp
    return client.post(f"/devices/{device}/disputes", json=body)


def approve(client, dispute_no="D1", device=DEVICE):
    return client.post(f"/devices/{device}/disputes/{dispute_no}/approve")


def reject(client, dispute_no="D1", device=DEVICE):
    return client.post(f"/devices/{device}/disputes/{dispute_no}/reject")


def get_dispute(client, dispute_no="D1", device=DEVICE):
    return client.get(f"/devices/{device}/disputes/{dispute_no}")


# ---------------------------------------------------------------- 申请

def test_file_correct_dispute_freezes_reading(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    r = file(client, ts="2026-10-01T08:05:00Z", temp=5.0)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending"
    assert body["action"] == "correct"
    assert body["target"] == {"sampled_at": "2026-10-01T08:05:00.000000Z",
                              "temperature": 5.0}
    assert body["reason"] == "sensor drift"
    assert body["frozen"] == {
        "sampled_at": "2026-10-01T08:00:00.000000Z",
        "temperature": 9.0,
        "revoked": False,
        "revision": 0,
    }
    assert body["decision"] is None and body["decided_at"] is None
    assert body["changes"] is None and body["expire_reason"] is None


def test_file_revoke_dispute(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    r = file(client, action="revoke", ts=None, temp=None)
    assert r.status_code == 201
    assert r.json()["action"] == "revoke"
    assert r.json()["target"] is None


def test_file_dispute_on_missing_reading_returns_404(client):
    assert file(client).status_code == 404


def test_file_dispute_on_revoked_reading_returns_409(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    client.delete(f"/devices/{DEVICE}/readings/E1")
    assert file(client).status_code == 409


def test_file_dispute_requires_non_empty_reason(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    assert file(client, reason="").status_code == 422
    assert file(client, reason="   ").status_code == 422


def test_file_dispute_validates_action_payload(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    # correct 缺少目标字段
    assert file(client, ts=None).status_code == 422
    assert file(client, temp=None).status_code == 422
    # revoke 不允许携带目标字段
    assert file(client, action="revoke").status_code == 422
    # 非法动作与非法温度
    assert file(client, action="modify").status_code == 422
    assert file(client, temp=-500).status_code == 422


def test_file_dispute_idempotent_replay_returns_original(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    first = file(client)
    assert first.status_code == 201
    replay = file(client)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    # 目标读数此后被直接改动，重放仍返回原申请
    client.put(f"/devices/{DEVICE}/readings/E1",
               json={"sampled_at": "2026-10-01T08:00:00Z", "temperature": 7.0})
    replay2 = file(client)
    assert replay2.status_code == 200
    assert replay2.json()["frozen"]["temperature"] == 9.0
    assert len(client.get(f"/devices/{DEVICE}/disputes").json()["disputes"]) == 1


def test_file_dispute_same_no_different_content_conflicts(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post_reading(client, "E2", "2026-10-01T08:10:00Z", 9.0)
    assert file(client).status_code == 201
    assert file(client, temp=6.0).status_code == 409
    assert file(client, reason="other reason").status_code == 409
    assert file(client, event_no="E2").status_code == 409
    assert file(client, action="revoke", ts=None, temp=None).status_code == 409


# ---------------------------------------------------------------- 批准

def test_approve_correct_executes_and_records_changes(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post_reading(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    file(client, event_no="E2", ts="2026-10-01T08:10:00Z", temp=5.0)
    r = approve(client)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "approved"
    assert body["decision"] == "approve"
    assert body["decided_at"] is not None
    # 更正生效：读数被改写，原区间失效，差异被记录
    assert [i["start_event_no"] for i in body["changes"]["invalidated"]] == ["E1"]
    readings = client.get(f"/devices/{DEVICE}/readings").json()["readings"]
    assert readings[1]["temperature"] == 5.0
    assert client.get(f"/devices/{DEVICE}/intervals").json()["intervals"] == []


def test_approve_revoke_executes(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post_reading(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    file(client, event_no="E2", action="revoke", ts=None, temp=None)
    body = approve(client).json()
    assert body["status"] == "approved"
    readings = client.get(f"/devices/{DEVICE}/readings").json()["readings"]
    assert readings[1]["revoked"] is True
    assert client.get(f"/devices/{DEVICE}/intervals").json()["intervals"] == []


def test_approve_can_create_interval_and_records_diff(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post_reading(client, "E2", "2026-10-01T08:10:00Z", 5.0)
    file(client, event_no="E2", ts="2026-10-01T08:10:00Z", temp=9.5)
    body = approve(client).json()
    assert body["status"] == "approved"
    assert [i["start_event_no"] for i in body["changes"]["added"]] == ["E1"]
    assert len(client.get(f"/devices/{DEVICE}/intervals").json()["intervals"]) == 1


def test_approve_retry_returns_first_result(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    file(client, temp=5.0)
    first = approve(client).json()
    again = approve(client)
    assert again.status_code == 200
    assert again.json() == first  # 含 decided_at 与 changes，完全一致
    # 读数只被改过一次：再次批准不会重复执行
    reading = client.get(f"/devices/{DEVICE}/readings").json()["readings"][0]
    assert reading["temperature"] == 5.0


# ---------------------------------------------------------------- 驳回

def test_reject_leaves_reading_untouched(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    file(client, temp=5.0)
    body = reject(client).json()
    assert body["status"] == "rejected"
    assert body["decision"] == "reject"
    assert body["changes"] == {"added": [], "changed": [], "invalidated": []}
    reading = client.get(f"/devices/{DEVICE}/readings").json()["readings"][0]
    assert reading["temperature"] == 9.0


def test_reject_retry_and_reverse_decision_conflict(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    file(client)
    first = reject(client).json()
    assert reject(client).json() == first  # 同一决定重试：返回首次结果
    assert approve(client).status_code == 409  # 反向决定冲突


def test_approve_then_reject_conflicts(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    file(client)
    approve(client)
    assert reject(client).status_code == 409


# ---------------------------------------------------------------- 过期

def test_approve_expires_after_direct_correction(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    file(client, temp=5.0)
    # 申请后被直接操作改写
    client.put(f"/devices/{DEVICE}/readings/E1",
               json={"sampled_at": "2026-10-01T08:00:00Z", "temperature": 7.0})
    body = approve(client).json()
    assert body["status"] == "expired"
    assert body["decision"] == "approve"
    assert body["expire_reason"]
    assert body["reason"] == "sensor drift"  # 原因保留
    # 不再改动读数：仍是直接操作后的值
    reading = client.get(f"/devices/{DEVICE}/readings").json()["readings"][0]
    assert reading["temperature"] == 7.0


def test_approve_expires_even_if_values_coincide(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    file(client, temp=5.0)
    # 直接操作两次后读数内容恰好回到原值，但修订号已变化
    client.put(f"/devices/{DEVICE}/readings/E1",
               json={"sampled_at": "2026-10-01T08:00:00Z", "temperature": 7.0})
    client.put(f"/devices/{DEVICE}/readings/E1",
               json={"sampled_at": "2026-10-01T08:00:00Z", "temperature": 9.0})
    body = approve(client).json()
    assert body["status"] == "expired"
    reading = client.get(f"/devices/{DEVICE}/readings").json()["readings"][0]
    assert reading["temperature"] == 9.0  # 未被执行争议目标值


def test_approve_expires_after_direct_revoke(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    file(client, temp=5.0)
    client.delete(f"/devices/{DEVICE}/readings/E1")
    body = approve(client).json()
    assert body["status"] == "expired"
    assert body["expire_reason"]


def test_approve_expires_after_other_dispute_executed(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    file(client, "D1", temp=5.0)
    file(client, "D2", temp=6.0)
    assert approve(client, "D1").json()["status"] == "approved"
    # 同一读数被在先争议执行改变，D2 过期
    body = approve(client, "D2").json()
    assert body["status"] == "expired"
    reading = client.get(f"/devices/{DEVICE}/readings").json()["readings"][0]
    assert reading["temperature"] == 5.0  # 只应用了 D1


def test_expired_is_terminal(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    file(client)
    client.put(f"/devices/{DEVICE}/readings/E1",
               json={"sampled_at": "2026-10-01T08:00:00Z", "temperature": 7.0})
    first = approve(client).json()
    assert first["status"] == "expired"
    assert approve(client).json() == first  # 同一决定重试：返回首次结果
    assert reject(client).status_code == 409  # 反向决定冲突


# ---------------------------------------------------------------- 查询

def test_get_dispute_shows_frozen_basis_and_outcome(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post_reading(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    file(client, event_no="E2", ts="2026-10-01T08:10:00Z", temp=5.0)
    approve(client)
    body = get_dispute(client).json()
    assert body["status"] == "approved"
    assert body["frozen"]["temperature"] == 9.5  # 冻结依据
    assert body["frozen"]["revision"] == 0
    assert body["changes"]["invalidated"][0]["start_event_no"] == "E1"  # 执行结果


def test_unknown_dispute_returns_404(client):
    assert get_dispute(client, "NOPE").status_code == 404
    assert approve(client, "NOPE").status_code == 404
    assert reject(client, "NOPE").status_code == 404


def test_list_disputes(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    file(client, "D1")
    file(client, "D2", action="revoke", ts=None, temp=None)
    approve(client, "D2")
    body = client.get(f"/devices/{DEVICE}/disputes").json()
    assert [d["dispute_no"] for d in body["disputes"]] == ["D1", "D2"]
    statuses = {d["dispute_no"]: d["status"] for d in body["disputes"]}
    assert statuses == {"D1": "pending", "D2": "approved"}


def test_disputes_are_isolated_per_device(client):
    post_reading(client, "E1", "2026-10-01T08:00:00Z", 9.0, device="dev-A")
    assert file(client, device="dev-A").status_code == 201
    assert get_dispute(client, device="dev-B").status_code == 404
    assert client.get("/devices/dev-B/disputes").json()["disputes"] == []
