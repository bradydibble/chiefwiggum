# Ralph Loop "Dying" Bug - Implementation Summary

## Status: ✅ COMPLETE

All three bugs from the investigation plan have been fixed.

---

## Bug #1: Task Continuation (CRITICAL) - ✅ FIXED

### Problem
Ralphs stopped after completing ONE task instead of automatically claiming the next task from the queue.

### Root Cause
The task continuation logic at lines 1291-1328 in `ralph_loop.sh` used `continue` and `break` statements inside the `execute_claude_code()` function. These statements don't work across function boundaries - they only affect loops in the current function, not the calling function's loop.

### Solution Implemented

#### Changes to ralph_loop.sh:

1. **Line 1316**: Changed `continue` to `return 4`
   - Signals main loop: "task complete, next task claimed, continue loop"

2. **Line 1320**: Changed `break` to `return 5`
   - Signals main loop: "failed to generate prompt, exit loop"

3. **Line 1327**: Changed `break` to `return 6`
   - Signals main loop: "no more tasks, exit gracefully"

4. **Lines 1802-1820**: Added handling for new return codes in main loop:
   ```bash
   elif [ $exec_result -eq 4 ]; then
       # Task completed and next task claimed - continue loop immediately
       continue
   elif [ $exec_result -eq 5 ]; then
       # Failed to generate prompt - exit loop
       break
   elif [ $exec_result -eq 6 ]; then
       # No more tasks - exit gracefully
       break
   ```

#### Changes to spawner.py:

5. **Lines 338-389**: Added `generate_prompt_for_task()` function
   - Wrapper function callable from bash scripts
   - Takes task_id string, fetches TaskClaim from database
   - Calls existing `generate_task_prompt()` to generate prompt
   - Handles async database access internally

### Expected Behavior After Fix

1. Ralph completes Task #1 ✓
2. Ralph marks task complete in database ✓
3. Ralph attempts to claim next task from queue ✓
4. If task claimed:
   - Generate new prompt for Task #2 ✓
   - Reset session state ✓
   - Continue loop with Task #2 ✓
5. If no more tasks:
   - Log "Queue empty - all tasks complete!" ✓
   - Exit gracefully ✓

---

## Bug #2: Premature Exit - ✅ ALREADY FIXED

### Problem
`MAX_CONSECUTIVE_DONE_SIGNALS=2` was too low, causing Ralphs to exit after just 2 loops when Claude signaled completion, even if the task wasn't actually finished.

### Status
Already fixed in the codebase at line 103:
```bash
MAX_CONSECUTIVE_DONE_SIGNALS=4  # Increased from 2 to 4 to prevent premature exits
```

### Reasoning
- Prevents premature exits on tasks requiring multiple iterations
- Allows Claude to signal progress/milestones without triggering exit
- Reduces false "Ralph died" incidents
- Still provides safety against infinite loops (combined with MAX_LOOPS)

---

## Bug #3: Task Release - ✅ ALREADY FIXED

### Problem
When Ralphs exited gracefully, tasks weren't properly released back to the database, leaving them in "claimed" state.

### Status
Already fixed in the codebase. The `release_current_task()` function (lines 1896-1918) is called on all exit paths:

1. **Line 1440**: Cleanup handler (SIGINT/SIGTERM) ✓
2. **Line 1538**: Max loops exceeded ✓
3. **Line 1597**: Cost budget exceeded ✓
4. **Line 1625**: Circuit breaker opened ✓
5. **Line 1653**: Graceful exit ✓
6. **Line 1754**: Circuit breaker trip (duplicate path) ✓

---

## Verification Checklist

### Manual Testing

- [ ] **Test Case 1: Multiple Tasks in Queue**
  1. Add 3+ tasks to the queue via ChiefWiggum
  2. Start a single Ralph instance
  3. Verify: Ralph completes Task #1, automatically claims Task #2
  4. Verify: Ralph completes Task #2, automatically claims Task #3
  5. Verify: Ralph completes all tasks, then exits gracefully
  6. Check logs for "Claimed next task from queue" messages
  7. Verify: All tasks marked complete in database

- [ ] **Test Case 2: Queue Empties After First Task**
  1. Add only 1 task to queue
  2. Start Ralph
  3. Verify: Ralph completes task
  4. Verify: Ralph checks for next task, finds none
  5. Verify: Ralph logs "Queue empty - all tasks complete!" and exits
  6. Verify: Task marked complete in database

- [ ] **Test Case 3: Prompt Generation Works**
  1. Start Ralph with 2+ tasks
  2. After first task completes, check PROMPT_FILE
  3. Verify: Prompt file updated with new task details
  4. Monitor second loop iteration
  5. Verify: Claude receives correct context for second task

### Database Verification

After each test, verify task status:
```bash
# Check if task is still claimed
wig status <ralph_id>

# Or query database directly
wig tasks --format=json | jq '.[] | select(.task_id == "TASK_ID") | .status'
```

### Log Verification

Check logs for these messages indicating successful operation:

1. Task completion:
   ```
   [SUCCESS] ✅ Task TASK_ID marked complete in database
   ```

2. Task claiming:
   ```
   [INFO] 📋 Checking for next task in queue...
   [SUCCESS] 📋 Claimed next task from queue: TASK_ID - TASK_TITLE
   ```

3. Prompt generation:
   ```
   [INFO] 📝 Generating prompt for new task...
   [SUCCESS] ✅ Prompt generated for task TASK_ID
   ```

4. Loop continuation:
   ```
   [INFO] 🔄 Continuing loop with next task...
   [SUCCESS] ✅ Next task claimed successfully, continuing loop with new task
   ```

5. Queue empty:
   ```
   [SUCCESS] 🎉 Queue empty - all tasks complete!
   [SUCCESS] 🎉 All tasks complete! Queue is empty.
   ```

---

## Files Modified

1. **chiefwiggum/scripts/ralph_loop.sh**
   - Fixed task continuation logic (return codes instead of continue/break)
   - Added handling for new return codes in main loop

2. **chiefwiggum/spawner.py**
   - Added `generate_prompt_for_task()` function for bash script compatibility

---

## Architecture Notes

### Task Continuation Flow

```
execute_claude_code() [ralph_loop.sh:1062-1432]
  └─> Detects task completion (line 1277)
      └─> Marks complete via wig (line 1288)
          └─> Attempts claim_next_task_for_ralph() (line 1294)
              ├─> Success: return 4 → main loop continues
              ├─> Prompt fail: return 5 → main loop exits
              └─> No tasks: return 6 → main loop exits gracefully

main() [ralph_loop.sh:1482-1809]
  └─> while true loop (line 1527)
      └─> execute_claude_code() (line 1675)
          └─> Check exec_result (lines 1740-1820)
              ├─> 0: Success, continue loop
              ├─> 2: API limit, ask user
              ├─> 3: Circuit breaker, exit
              ├─> 4: Next task claimed, continue immediately
              ├─> 5: Prompt generation failed, exit
              ├─> 6: All tasks done, exit gracefully
              └─> else: Failed, retry after 30s
```

### Key Functions

- `claim_next_task_for_ralph()` (line 1921): Claims next task from queue
- `get_current_task_id()` (line 1937): Gets task ID for Ralph instance
- `get_task_title()` (line 1946): Gets task title from database
- `generate_task_prompt_for_file()` (line 1965): Generates prompt, calls spawner.py
- `release_current_task()` (line 1896): Releases task claim
- `generate_prompt_for_task()` (spawner.py:338): Python function for prompt generation

---

## Expected User Impact

### Before Fix
- Ralph processes only ONE task then stops
- Queue never gets processed beyond first task
- User must manually restart Ralph for each task
- "Keep chugging through tasks" workflow doesn't work
- Appears as if Ralph "died" after one task

### After Fix
- Ralph automatically processes ALL tasks in queue
- Seamless task-to-task transitions
- Only exits when queue is empty
- Proper "worker" behavior as intended
- Clear log messages for each state transition

---

## Rollback Instructions

If issues arise, revert changes:

```bash
git checkout HEAD -- chiefwiggum/scripts/ralph_loop.sh
git checkout HEAD -- chiefwiggum/spawner.py
```

Then manually release any stuck tasks:
```bash
wig release <ralph_id>
```

---

## Next Steps

1. Test the implementation with the verification checklist above
2. Monitor Ralph logs during multi-task execution
3. Verify database task states remain consistent
4. Consider adding metrics for:
   - Average tasks per Ralph session
   - Task claiming success rate
   - Time between task transitions

---

## Additional Notes

- The `MAX_CONSECUTIVE_DONE_SIGNALS=4` threshold may need tuning based on observed behavior
- Consider making this threshold configurable per task type or priority
- The task continuation logic is now properly isolated via return codes, making it easier to debug and maintain
- Prompt generation is delegated to spawner.py for consistency with initial task spawning
