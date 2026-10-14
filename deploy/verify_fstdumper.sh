#!/usr/bin/env bash
# Verify a built fstdumper.so on Xcelium.
#
# Runs deploy/fstdumper_verify/vpi_coverage_tb.sv once, with fstdumper
# (driven by examples/xcelium_fst/fst_dump_cfg.sv) and Xcelium's native VCD
# dumper in the SAME simulation, then compares every signal transition by
# transition. Also checks that the log has no VPI errors or FST warnings,
# that no signal is declared twice and that port-collapsed nets were
# written as FST aliases (both only hold for a patched build).
#
# Needs: xrun (with a license), fst2vcd from GTKWave, python3.
#
# Usage: bash deploy/verify_fstdumper.sh --so <fstdumper.so> [--out DIR] [-- xrun options]
#        FSTDUMPER_SO=<fstdumper.so> bash deploy/verify_fstdumper.sh
# Output: logs, FST and baseline VCD in DIR (default: a new directory under
#         ${TMPDIR:-/tmp}); the last line is "[verify] PASS: ..." or
#         "[verify] FAIL".
#
# Reference result (Xcelium 24.09, 25.09, 26.05 and 26.08 all give it):
#   [verify] PASS: 93 signals identical (717 transitions), 16 parameters
#   not dumped, 9 extra in FST
# The 16 are 15 genvars and 1 localparam (fstdumper never dumps constants);
# the 9 extra are static task variables, which Xcelium's VCD dumper omits.
#
# Part of wave-mcp (Apache-2.0).
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
TB="$HERE/fstdumper_verify/vpi_coverage_tb.sv"
CMP="$HERE/fstdumper_verify/compare_fst_vcd.py"
CFG="$REPO_ROOT/examples/xcelium_fst/fst_dump_cfg.sv"
SO="${FSTDUMPER_SO:-}"
OUT=""
XRUN_EXTRA=()

fail() { echo "[verify] $*"; echo "[verify] FAIL"; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --so)   [ $# -ge 2 ] || fail "--so needs a path"; SO="$2"; shift ;;
        --out)  [ $# -ge 2 ] || fail "--out needs a directory"; OUT="$2"; shift ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        --)     shift; XRUN_EXTRA=("$@"); break ;;
        *)      fail "unknown argument: $1 (see --help)" ;;
    esac
    shift
done

[ -n "$SO" ] || fail "no fstdumper.so given (--so PATH or FSTDUMPER_SO)"
[ -f "$SO" ] || fail "not found: $SO (build it with deploy/build_fstdumper.sh)"
SO="$(cd "$(dirname "$SO")" && pwd)/$(basename "$SO")"
for f in "$TB" "$CMP" "$CFG"; do [ -f "$f" ] || fail "missing $f"; done
command -v xrun    >/dev/null 2>&1 || fail "xrun not in PATH"
command -v fst2vcd >/dev/null 2>&1 || fail "fst2vcd not in PATH (it ships with GTKWave)"
command -v python3 >/dev/null 2>&1 || fail "python3 not in PATH"

if [ -z "$OUT" ]; then
    OUT="$(mktemp -d "${TMPDIR:-/tmp}/fstdumper-verify.XXXXXX")" || fail "cannot create a work directory"
else
    mkdir -p "$OUT" || fail "cannot create $OUT"
fi
OUT="$(cd "$OUT" && pwd)"
cd "$OUT"

cat > baseline.tcl <<'EOF'
database -open vcddb -vcd -into baseline.vcd -default
probe -create vpi_coverage_tb -all -depth all -database vcddb
run
exit
EOF

echo "[verify] fstdumper: $SO"
echo "[verify] work dir : $OUT"
xrun -64bit -sv -access +r -xmlibdirname xcl \
     -loadvpi "$SO:vlog_startup_routines_bootstrap" \
     "$TB" "$CFG" -top vpi_coverage_tb -top fst_dump \
     -define FST_DUMP_TOP=vpi_coverage_tb -define 'FST_DUMP_FILE="probe.fst"' \
     -input baseline.tcl ${XRUN_EXTRA[@]+"${XRUN_EXTRA[@]}"} \
     -l xrun.log < /dev/null > /dev/null 2>&1
rc=$?
grep -E '^\[fst_dump\]|^FST' xrun.log | sed 's/^/[verify]   /'
[ $rc -eq 0 ] || fail "xrun exit code $rc, see $OUT/xrun.log"
[ -f probe.fst ] && [ -f baseline.vcd ] || fail "probe.fst / baseline.vcd not written, see $OUT/xrun.log"
if grep -q -E 'VPI +[A-Z]+|Unsupported argument|^FST (warning|Error)' xrun.log; then
    grep -E 'VPI +[A-Z]+|Unsupported argument|^FST (warning|Error)' xrun.log | head -5
    fail "VPI errors / FST warnings in xrun.log (unpatched or outdated build?)"
fi

fst2vcd -f probe.fst -o probe_fst.vcd > /dev/null 2>&1 || fail "fst2vcd failed"
dups=$(awk '/^\$scope/{s[++d]=$3; next} /^\$upscope/{d--; next}
            /^\$var/{p=""; for (i=1;i<=d;i++) p=p s[i] "."; k=p $5; if (seen[k]++) n++}
            /^\$enddefinitions/{print n+0; exit}' probe_fst.vcd)
[ "$dups" = "0" ] || fail "$dups signals declared more than once in probe.fst (outdated build?)"
echo "[verify]   no duplicate \$var"
grep -q 'of them share the value of a port-collapsed net' xrun.log \
    || fail "no port-collapsed net aliases reported (unpatched or outdated build?)"

python3 "$CMP" probe_fst.vcd baseline.vcd > compare.txt 2>&1 \
    || { cat compare.txt; fail "compare_fst_vcd.py failed"; }
sed 's/^/[verify]   /' compare.txt | head -12
python3 "$CMP" --verdict probe_fst.vcd baseline.vcd
