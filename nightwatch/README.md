# Nightwatch package

User documentation:
- ../../README.md
- ../../README_CN.md

Core invariants:
- trusted state outside workspace
- exact-thread only
- no --last
- frozen verification authority
- fail-closed identity
- one supervised writer per workspace
- multiple runs require distinct repos/worktrees
