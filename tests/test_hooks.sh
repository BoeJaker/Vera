#!/bin/sh
# Repo-hygiene guardrail hooks test (documentation/specs/dev-lifecycle-and-repo-hygiene.md §3).
# Builds a throwaway repo, installs this repo's tools/hooks, and asserts every
# guardrail fires (or doesn't) as intended. Run: sh tests/test_hooks.sh   (exit 0 = all pass)
# secret-scan (check 4) is stubbed here to isolate the branch/author/message guardrails;
# secret-scan has its own coverage and runs unchanged in the real hook.
set -u
SRC=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "not in a git repo"; exit 2; }
T=$(mktemp -d); trap 'cd /; rm -rf "$T"' EXIT
cd "$T" || exit 2
git init -q -b feat/init
git config user.name "Jane Dev"; git config user.email "jane@example.com"
cp -r "$SRC/tools" tools
printf 'import sys\nsys.exit(0)\n' > tools/secret_scan.py           # stub secret-scan
git config core.hooksPath tools/hooks
chmod +x tools/hooks/pre-commit tools/hooks/commit-msg
pass=0; fail=0
chk(){ if { [ "$2" = block ] && [ "$3" -ne 0 ]; } || { [ "$2" = allow ] && [ "$3" -eq 0 ]; }; \
       then echo "  ok   $1"; pass=$((pass+1)); else echo "  FAIL $1 (want $2, exit=$3)"; fail=$((fail+1)); fi; }
reset(){ git reset -q --hard 2>/dev/null; git switch -q feat/init 2>/dev/null; git clean -qfd 2>/dev/null; }

echo base>base.txt; git add base.txt tools; git commit -qm "chore: base" >/dev/null 2>&1
chk "valid typed-branch commit -> allow" allow $?
git branch main
git switch -q main; echo x>>base.txt; git add base.txt; git commit -qm "d" >/dev/null 2>&1
chk "direct commit to main -> block" block $?; reset
git switch -q main; echo y>y.txt; git add y.txt; VERA_ALLOW_MAIN_COMMIT=1 git commit -qm "deploy" >/dev/null 2>&1
chk "main + VERA_ALLOW_MAIN_COMMIT -> allow" allow $?; reset
git switch -qc badname; echo z>z.txt; git add z.txt; git commit -qm "b" >/dev/null 2>&1
chk "untyped branch name -> block" block $?; reset; git branch -qD badname
git switch -qc feat/ai; echo a>a.txt; git add a.txt; GIT_AUTHOR_NAME=Claude GIT_AUTHOR_EMAIL=noreply@anthropic.com git commit -qm "x" >/dev/null 2>&1
chk "AI git author -> block" block $?; reset; git branch -qD feat/ai
git switch -qc feat/tr; echo b>b.txt; git add b.txt
printf 'feat: x\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n' > cm
git commit -qF cm >/dev/null 2>&1
chk "Co-Authored-By: Claude trailer -> block" block $?; reset; git branch -qD feat/tr
git switch -qc feat/m; echo m>m.txt; git add m.txt; git commit -qm "feat: m" >/dev/null 2>&1
git switch -q main; git merge --no-ff -m "merge feat/m" feat/m >/dev/null 2>&1
chk "merge commit onto main -> allow" allow $?

echo "RESULT pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
