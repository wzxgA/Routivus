"""HITLPolicy 单元测试：审批决策、敏感度、fail closed。"""

from __future__ import annotations

import pytest

from routivus.safety.hitl import ApprovalDecision, HITLPolicy


class TestSensitivity:
    def test_default_levels(self):
        policy = HITLPolicy()
        assert policy.sensitivity("read_file") == "never"
        assert policy.sensitivity("write_file") == "confirm"
        assert policy.sensitivity("execute_command") == "always"

    def test_custom_levels_override(self):
        policy = HITLPolicy(levels={"write_file": "never"})
        assert policy.sensitivity("write_file") == "never"


class TestDecide:
    async def test_never_tool_auto_allowed(self):
        policy = HITLPolicy()
        decision = await policy.decide("read_file", {"path": "a.py"})
        assert decision.allow
        assert decision.reason == "auto_allow"

    async def test_disabled_policy_auto_allows(self):
        policy = HITLPolicy(enabled=False)
        decision = await policy.decide("execute_command", {"command": "dir"})
        assert decision.allow

    async def test_fail_closed_without_requester(self):
        """策略启用但无审批回调：需审批的操作一律拒绝。"""
        policy = HITLPolicy()
        decision = await policy.decide("execute_command", {"command": "dir"})
        assert not decision.allow
        assert decision.reason == "auto_deny_no_requester"

    async def test_requester_called_and_decision_used(self):
        async def fake_requester(tool_name, level, args):
            return ApprovalDecision(allow=True, reason="user_approved")

        policy = HITLPolicy(requester=fake_requester)
        decision = await policy.decide("execute_command", {"command": "dir"})
        assert decision.allow
        assert decision.reason == "user_approved"

    async def test_requester_rejection(self):
        async def fake_requester(tool_name, level, args):
            return ApprovalDecision(allow=False, reason="user_rejected")

        policy = HITLPolicy(requester=fake_requester)
        assert not (await policy.decide("execute_command", {"command": "dir"})).allow


class TestAllowAll:
    async def test_session_allow_all_bypasses_approval(self):
        async def fake_requester(tool_name, level, args):
            return ApprovalDecision(allow=False, reason="user_rejected")

        policy = HITLPolicy(requester=fake_requester)
        policy.allow_all()
        decision = await policy.decide("execute_command", {"command": "dir"})
        assert decision.allow
        assert decision.reason == "auto_allow"

    def test_reset_session(self):
        policy = HITLPolicy()
        policy.allow_all()
        assert policy.session_allow_all
        policy.reset_session()
        assert not policy.session_allow_all

    def test_set_enabled(self):
        policy = HITLPolicy()
        policy.set_enabled(False)
        assert not policy.enabled
        policy.set_enabled(True)
        assert policy.enabled
