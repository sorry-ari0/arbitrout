# Dispatcher Instructions

When the dispatcher sends you a task number, follow these steps:

1. Read ONLY that task from tasks.md (use read tool with offset/limit — do NOT load the full file)
2. Create a feature branch: `git checkout -b feature/task-<number>`
3. Read ONLY the files listed in the task — do not read unrelated files
4. Make the changes described in the task
5. Test: `curl.exe -s http://127.0.0.1:8501/api/arbitrage/opportunities`
6. Commit with a descriptive message
7. Push: `git push -u origin feature/task-<number>`
8. Create PR: `gh pr create --title "Task <number> - <short description>" --body "Automated PR from OpenClaw"`
9. Update tasks.md: change the task status from TODO to COMPLETED
10. Update project.md Completed Features section

## If stuck
- After 2 failed attempts on the same step, mark task as BLOCKED in tasks.md
- Write the error to .learnings/ folder for future reference
- Reply with BLOCKED

## Rules
- NEVER push directly to main — always use feature branches and PRs
- NEVER modify repos outside this project directory
- NEVER load full files when you only need a section
- Reply COMPLETED when done, BLOCKED if stuck
