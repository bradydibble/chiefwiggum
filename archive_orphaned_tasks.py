#!/usr/bin/env python3
"""Archive orphaned completed tasks that are no longer in @fix_plan.md.

This script marks the 21 tasks that were completed but removed from @fix_plan.md
as archived, so they won't cause reconciliation failures.
"""

import asyncio

from chiefwiggum import archive_task, init_db


ORPHANED_TASK_IDS = [
    "task-21-google-drive-integration",
    "task-22-file-processing-workflow",
    "task-23-slash-commands",
    "task-24-decision-revisit-conditions",
    "task-25-prior-day-reconciliation",
    "task-26-data-freshness-tracking",
    "task-27-assist-button",
    "task-28-standing-approvals-registry",
    "task-29-weekly-review-automation",
    "task-30-extension-scripts-pattern",
    "task-35-pf-1-timezone-to-pacific",
    "task-36-pf-2-reject-button-broken",
    "task-37-pf-3-slack-ids-to-names",
    "task-38-pf-4-todo-modal-source-attribution",
    "task-39-pf-5-rejection-reason-selection",
    "task-40-pf-6-relevance-filtering-not-relevant-to-me",
    "task-41-pf-7-integration-visibility-fathom-gsuite",
    "task-42-pf-8-audit-log",
    "task-43-pf-9-todo-project-linking",
    "task-44-pf-10-text-truncation-causing-noise",
    "task-45-pf-11-context-capture-see-above-messages",
]


async def main():
    """Archive all orphaned tasks."""
    print("Archiving orphaned completed tasks...\n")

    # Initialize database
    await init_db()

    archived_count = 0
    failed_count = 0

    for task_id in ORPHANED_TASK_IDS:
        success = await archive_task(task_id)
        if success:
            print(f"✅ Archived: {task_id}")
            archived_count += 1
        else:
            print(f"❌ Failed:   {task_id} (not found or not completed)")
            failed_count += 1

    print(f"\n{'='*60}")
    print(f"Summary:")
    print(f"  Archived: {archived_count}")
    print(f"  Failed:   {failed_count}")
    print(f"  Total:    {len(ORPHANED_TASK_IDS)}")
    print(f"{'='*60}")

    if archived_count > 0:
        print("\n✅ Orphaned tasks archived successfully!")
        print("   These tasks will no longer cause reconciliation failures.")

    if failed_count > 0:
        print(f"\n⚠️  {failed_count} task(s) could not be archived.")
        print("   They may have already been archived or don't exist.")


if __name__ == "__main__":
    asyncio.run(main())
