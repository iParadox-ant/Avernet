"""PatchPlanner behaviour: LLM-disabled skip + generate_and_save_patches persistence."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, AsyncMock

from agentclaw.community.core.harness.services.patch_planner import PatchPlanner


def _planner(llm):
    return PatchPlanner(
        patch_library=MagicMock(),
        llm=llm,
        bot_profile=MagicMock(),
        patch_record_repo=MagicMock(),
        patch_repo=MagicMock(),
        scan_record_repo=MagicMock(),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_llm_disabled_fix_returns_none():
    llm = MagicMock()
    llm.chat = AsyncMock(return_value="[llm disabled]")
    planner = _planner(llm)
    out = await planner._llm_generate_fix(
        file_type="AGENTS.md", src="# original",
        issues="- missing summary\n- weak rules", template_instructions="",
    )
    # LLM unavailable (no token / retries exhausted) → skip cleanly; never
    # fabricate a no-op patch (dst==src) that masquerades as a fix.
    assert out is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_llm_exception_returns_none():
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=RuntimeError("net down"))
    out = await _planner(llm)._llm_generate_fix(
        file_type="AGENTS.md", src="x", issues="- y", template_instructions="",
    )
    assert out is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_todo_only_patch_returns_none():
    """When the LLM's only change is `<!-- TODO: ... -->` placeholders (it was
    asked to "supplement info" but given no concrete content), the result is not
    an actionable fix — drop it like a no-op patch rather than persisting TODO
    noise."""
    llm = MagicMock()
    llm.chat = AsyncMock(
        return_value=(
            "<!-- FIX_SUMMARY: 待补充画像信息 -->\n"
            "# SOUL\n\n"
            "<!-- TODO: 补充当前 Bot 的专属职责与场景 -->\n"
        )
    )
    out = await _planner(llm)._llm_generate_fix(
        file_type="SOUL.md", src="# SOUL",
        issues="- [info] D-SOUL-001: 建议补充画像信息",
        template_instructions="",
    )
    assert out is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_real_fix_returns_content():
    """A genuine content change (not just TODO markers) is returned as a patch."""
    llm = MagicMock()
    llm.chat = AsyncMock(
        return_value="<!-- FIX_SUMMARY: 重写人格描述 -->\n# SOUL\n\n我是当前 Bot 的专属人格。"
    )
    out = await _planner(llm)._llm_generate_fix(
        file_type="SOUL.md", src="# SOUL",
        issues="- [info] D-SOUL-001: 人格过于通用", template_instructions="",
    )
    assert out is not None
    fixed, desc = out
    assert "专属人格" in fixed
    assert desc == "重写人格描述"


# ── generate_and_save_patches: patch_repo write + scan_record update ──────────
#
# These exercise the now-unconditional persistence path (patch_repo /
# scan_record_repo are required deps, so the old None-guards are gone). The LLM
# leg (_generate_patch_operations) is stubbed so the test stays focused on
# persistence.

def _planner_for_generate(patch_repo, scan_record_repo):
    from agentclaw.community.core.harness.models import Layer, PatchOperation

    planner = PatchPlanner(
        patch_library=MagicMock(),
        llm=MagicMock(),
        bot_profile=MagicMock(),
        patch_record_repo=MagicMock(),
        patch_repo=patch_repo,
        scan_record_repo=scan_record_repo,
    )
    tpl = MagicMock()
    tpl.id, tpl.name, tpl.description, tpl.layer = 1, "tpl", "desc", Layer.L1
    tpl.operations = [PatchOperation(op="update_md", target="AGENTS.md", detail={})]
    planner._lib.get_template_by_id.return_value = tpl
    processed_op = PatchOperation(
        op="update_md", target="AGENTS.md", detail={"dst_content": "new"}
    )
    planner._generate_patch_operations = AsyncMock(
        return_value=([processed_op], "orig", "short")
    )
    planner._create_patch_record = MagicMock(return_value=MagicMock())
    return planner


def _report_with_template_finding():
    from agentclaw.community.core.harness.models import (
        Finding,
        FindingsReport,
        Severity,
    )

    report = FindingsReport(bot_id="b", entity_id="e")
    report.findings = [
        Finding(
            rule_id="r1", rule_name="R1", severity=Severity.WARNING,
            file_type="AGENTS.md", message="m", suggested_template_ids=[1],
        )
    ]
    return report


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generate_and_save_patches_writes_patch_and_updates_scan_record():
    patch_repo = MagicMock()
    patch_repo.create.return_value = 42
    scan_record_repo = MagicMock()
    planner = _planner_for_generate(patch_repo, scan_record_repo)

    out = await planner.generate_and_save_patches(
        _report_with_template_finding(), "staff", "e", "b", scan_id=99
    )

    patch_repo.create.assert_called_once()
    # patch_id 42 recorded → scan_record enriched
    scan_record_repo.update_patch_ids.assert_called_once()
    assert len(out) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generate_and_save_patches_swallows_patch_repo_write_error():
    patch_repo = MagicMock()
    patch_repo.create.side_effect = RuntimeError("db down")
    scan_record_repo = MagicMock()
    planner = _planner_for_generate(patch_repo, scan_record_repo)

    # create() raising is caught and logged; patch_id stays 0, so no finding→patch
    # mapping accrues and the scan_record update is skipped — but no exception escapes.
    out = await planner.generate_and_save_patches(
        _report_with_template_finding(), "staff", "e", "b", scan_id=99
    )

    patch_repo.create.assert_called_once()
    scan_record_repo.update_patch_ids.assert_not_called()
    assert len(out) == 1


