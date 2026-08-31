/*
 * fsdb2fst.cpp : Synopsys FSDB -> GTKWave FST single-pass converter.
 *
 * Pipeline: ffrAPI (libnffr.so, Verdi FsdbReader) reads the FSDB, fstapi
 * (MIT, vendored from gtkwave) writes the FST. No VCD intermediate, so the
 * 20 GB-class VCD detour never hits the disk.
 *
 * Build (see deploy/build_fsdb2fst.sh):
 *   g++ -O2 -std=c++17 -I$VERDI_HOME/share/FsdbReader \
 *       -o fsdb2fst fsdb2fst.cpp fstapi.c lz4.c fastlz.c jrb.c \
 *       -L$VERDI_HOME/share/FsdbReader/linux64 \
 *       -lnffr -lnsys -lz -lpthread -ldl \
 *       -Wl,-rpath,'$ORIGIN'
 *
 * License: this file is MIT (wave-mcp). It links at build time against the
 * Verdi FsdbReader libraries (libnffr.so / libnsys.so), which are NOT
 * redistributed here; the binary is a local artifact and never enters the
 * public repo or PyPI (see docs/THIRD_PARTY.md).
 *
 * Time model (fail-loud, pass-through like vcd2fst):
 *   FSDB stores tick counts; true_time = tick * scale, scale comes from
 *   ffrGetScaleUnit() (e.g. "1ns"). FST time values are likewise raw counts
 *   of 10^timescale seconds (the reader never rescales; fstWriterEmitTimeChange
 *   takes ticks of the header unit). So we emit the FSDB tick unchanged and
 *   set the FST header timescale to the same 10^-N unit, preserving exact
 *   values with no multiplication and no overflow. If the scale cannot be
 *   parsed, the conversion aborts instead of guessing a unit.
 *
 * Value model:
 *   ffrAPI hands per-bit byte codes (FSDB_BT_VCD_0/1/X/Z) for 1-byte-per-bit
 *   vars; fstapi's EmitValueChange takes one ASCII char per bit for non-real
 *   vars (verified in fstWriterEmitValueChange), so the mapping is 1:1.
 *   Real vars (bytes_per_bit 4B/8B) are emitted as a double. Unknown byte
 *   codes map to 'x'. Strength vars (2 bytes per bit) are rejected up front,
 *   matching wave-mcp's existing strength limitation.
 *
 * Loading model:
 *   All selected signals are loaded in-core in one shot (ffrAddToSignalList +
 *   ffrLoadSignals), then traversed through a single merged time-based
 *   iterator (ffrCreateTimeBasedVCTrvsHdl), which yields (idcode, time,
 *   value) records in time order. RAM scales with the design's value data;
 *   use -l / -L to convert a subset when the design is huge.
 */
#ifndef FFR_API_INCLUDE
#define FFR_API_INCLUDE "ffrAPI.h"
#endif

#include FFR_API_INCLUDE
#include "fstapi.h"

#include <cctype>
#include <cerrno>
#include <cinttypes>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {

const char *kVersion = "fsdb2fst 0.1.0 (wave-mcp)";

int g_verbose = 0;

void vlog(const char *fmt, ...) {
    if (!g_verbose) return;
    va_list ap;
    va_start(ap, fmt);
    fputs("[fsdb2fst] ", stderr);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
}

void vinfo(const char *fmt, ...) {  /* printed regardless of verbosity */
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
}

[[noreturn]] void die(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    fputs("[fsdb2fst] ERROR: ", stderr);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    std::exit(2);
}

/* ---------- FSDB scale string ("1ns", "100fs", ...) -> fs per tick ----------
 * Same contract as the TraceWeave wrapper: unparseable -> 0, callers must
 * abort rather than assume a unit. */

unsigned long long ParseScaleFs(const char *s) {
    if (!s || !*s) return 0;
    char *endp = NULL;
    double num = std::strtod(s, &endp);
    if (endp == s) num = 1.0;  /* bare unit like "ps" == "1ps" */
    while (*endp == ' ' || *endp == '\t') endp++;

    char unit[8];
    size_t n = 0;
    for (; endp[n] && n + 1 < sizeof(unit); n++)
        unit[n] = static_cast<char>(std::tolower(static_cast<unsigned char>(endp[n])));
    unit[n] = '\0';

    double mult;
    if      (!std::strcmp(unit, "fs")) mult = 1e0;
    else if (!std::strcmp(unit, "ps")) mult = 1e3;
    else if (!std::strcmp(unit, "ns")) mult = 1e6;
    else if (!std::strcmp(unit, "us")) mult = 1e9;
    else if (!std::strcmp(unit, "ms")) mult = 1e12;
    else if (!std::strcmp(unit, "s"))  mult = 1e15;
    else return 0;

    double fs = num * mult;
    if (fs < 1.0 || fs > 9e18) return 0;
    return static_cast<unsigned long long>(fs + 0.5);
}

/* FSDB var/scope enums -> fstapi enums
 *
 * The FSDB var-type enum follows the classic VCD keyword order
 * (0=event 1=integer 2=parameter 3=real 4=reg 5=supply0 6=supply1 7=time
 *  8=tri 9=triand 10=trior 11=trireg 12=tri0 13=tri1 14=wand 15=wire
 *  16=wor, then FSDB extras like 17=memory). fstapi's enum inserts
 * REAL_PARAMETER at 4, so translate explicitly instead of casting. */

fstVarType MapVarType(unsigned int t) {
    switch (t) {
    case 0:  return FST_VT_VCD_EVENT;
    case 1:  return FST_VT_VCD_INTEGER;
    case 2:  return FST_VT_VCD_PARAMETER;
    case 3:  return FST_VT_VCD_REAL;
    case 4:  return FST_VT_VCD_REG;
    case 5:  return FST_VT_VCD_SUPPLY0;
    case 6:  return FST_VT_VCD_SUPPLY1;
    case 7:  return FST_VT_VCD_TIME;
    case 8:  return FST_VT_VCD_TRI;
    case 9:  return FST_VT_VCD_TRIAND;
    case 10: return FST_VT_VCD_TRIOR;
    case 11: return FST_VT_VCD_TRIREG;
    case 12: return FST_VT_VCD_TRI0;
    case 13: return FST_VT_VCD_TRI1;
    case 14: return FST_VT_VCD_WAND;
    case 15: return FST_VT_VCD_WIRE;
    case 16: return FST_VT_VCD_WOR;
    default: return FST_VT_VCD_WIRE;  /* FSDB extras (memory, ...) */
    }
}

fstVarDir MapVarDir(unsigned int d) {
    /* FSDB: 0=IMPLICIT 1=INPUT 2=OUTPUT 3=INOUT 4=BUFFER 5=LINKAGE, which
     * matches fstapi's FST_VD_* numbering exactly. */
    if (d > 5) return FST_VD_IMPLICIT;
    return static_cast<fstVarDir>(d);
}

/* ---------- signal model ---------- */

struct Signal {
    fsdbVarIdcode id = 0;
    unsigned int len = 0;          /* width in bits */
    unsigned int bytes_per_bit = 0;
    unsigned int direction = 0;
    unsigned int var_type = 0;
    std::string name;              /* leaf name */
    std::vector<std::string> scope;
    std::string path;              /* dot-joined scope + name */
    bool is_real = false;
    bool loadable = true;          /* false for strength vars (rejected) */
    fstHandle fh = 0;              /* fstapi handle once created */
};

/* ---------- FSDB reader: tree walk + signal table ---------- */

struct FsdbReader {
    ffrObject *obj = nullptr;
    unsigned long long scale_fs = 0;
    std::string scale_unit;
    std::vector<Signal> signals;
    std::vector<std::string> scope_stack;
    unsigned int n_strength = 0;

    static bool_T TreeCB(fsdbTreeCBType cb_type, void *client_data,
                         void *tree_cb_data) {
        FsdbReader *r = static_cast<FsdbReader *>(client_data);
        switch (cb_type) {
        case FSDB_TREE_CBT_SCOPE: {
            fsdbTreeCBDataScope *s =
                static_cast<fsdbTreeCBDataScope *>(tree_cb_data);
            r->scope_stack.push_back(s->name ? s->name : "");
            break;
        }
        case FSDB_TREE_CBT_UPSCOPE:
            if (!r->scope_stack.empty()) r->scope_stack.pop_back();
            break;
        case FSDB_TREE_CBT_VAR: {
            fsdbTreeCBDataVar *v =
                static_cast<fsdbTreeCBDataVar *>(tree_cb_data);
            Signal sig;
            sig.id = v->u.idcode;
            sig.len = (v->lbitnum >= v->rbitnum)
                          ? (v->lbitnum - v->rbitnum + 1)
                          : (v->rbitnum - v->lbitnum + 1);
            if (sig.len == 0) sig.len = 1;
            sig.bytes_per_bit = v->bytes_per_bit ? v->bytes_per_bit : 1;
            sig.direction = static_cast<unsigned int>(v->direction);
            sig.var_type = static_cast<unsigned int>(v->type);
            sig.name = v->name ? v->name : "";
            sig.scope = r->scope_stack;
            std::string path;
            for (const auto &p : sig.scope) {
                if (p.empty()) continue;
                path += p;
                path += '.';
            }
            path += sig.name;
            sig.path = path;

            if (sig.bytes_per_bit == FSDB_BYTES_PER_BIT_4B ||
                sig.bytes_per_bit == FSDB_BYTES_PER_BIT_8B) {
                sig.is_real = true;
            } else if (sig.bytes_per_bit > FSDB_BYTES_PER_BIT_1B) {
                sig.loadable = false;  /* strength (2B): out of scope */
                r->n_strength++;
            }
            r->signals.push_back(std::move(sig));
            break;
        }
        default:
            break;
        }
        return TRUE;
    }

    void Open(const char *fname) {
        if (!ffrObject::ffrIsFSDB((str_T)fname))
            die("not an FSDB file: %s", fname);
        obj = ffrObject::ffrOpen3((str_T)fname);
        if (!obj) die("ffrOpen3 failed: %s", fname);

        str_T su = obj->ffrGetScaleUnit();
        if (su && su[0]) scale_unit = su;
        scale_fs = ParseScaleFs(scale_unit.c_str());
        if (scale_fs == 0)
            die("cannot parse the FSDB time scale (ffrGetScaleUnit = \"%s\"); "
                "refusing to guess a unit. Please report this file.",
                scale_unit.c_str());
        vlog("scale: \"%s\" -> %llu fs per tick", scale_unit.c_str(), scale_fs);

        obj->ffrSetTreeCBFunc(&FsdbReader::TreeCB, this);
        obj->ffrReadScopeVarTree();

        /* de-dup by full path (keep first) */
        std::set<std::string> seen;
        std::vector<Signal> uniq;
        uniq.reserve(signals.size());
        for (auto &s : signals) {
            if (!seen.insert(s.path).second) continue;
            uniq.push_back(std::move(s));
        }
        signals.swap(uniq);
    }

    void Close() {
        if (!obj) return;
        obj->ffrClose();
        obj = nullptr;
    }
};

/* ---------- CLI filter ---------- */

struct Filter {
    std::vector<std::string> substrs;   /* -l */
    std::set<std::string> exact;        /* -L */

    bool Passes(const std::string &path) const {
        if (!exact.empty() && !exact.count(path)) return false;
        for (const auto &k : substrs)
            if (path.find(k) == std::string::npos) return false;
        return true;
    }
};

void Usage() {
    std::fputs(
        "usage: fsdb2fst [options] <input.fsdb> [output.fst]\n"
        "\n"
        "Convert a Synopsys FSDB waveform to FST in a single pass, using the\n"
        "Verdi FsdbReader runtime (no license checkout, no VCD intermediate).\n"
        "If <output.fst> is omitted it defaults to <input>.fst.\n"
        "\n"
        "options:\n"
        "  -l LIST    only load signals whose full path contains one of the\n"
        "             comma-separated substrings LIST (e.g. -l u_core,uart)\n"
        "  -L FILE    only load signals whose full path is listed in FILE\n"
        "             (one path per line, '#' starts a comment)\n"
        "  -p PACK    FST packing: lz4 (default) | fastlz | zlib\n"
        "  --info     print file/scale/signal summary only, no conversion\n"
        "  --allow-empty  succeed even when no value data could be loaded\n"
        "  -v         verbose progress on stderr\n"
        "  -h         this help\n"
        "\n"
        "notes:\n"
        "  * strength-valued vars (2 bytes per bit) are skipped.\n"
        "  * all selected signals are loaded into RAM at once; for very\n"
        "    large designs convert per-scope with -l / -L.\n",
        stderr);
}

}  // namespace

/* ============================ main ============================ */

int main(int argc, char **argv) {
    if (std::getenv("FSDB2FST_VERBOSE")) g_verbose = 1;

    const char *in_path = nullptr;
    std::string out_arg;
    Filter filter;
    const char *pack = "lz4";
    bool info_only = false;
    bool allow_empty = false;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (!std::strcmp(a, "-v")) {
            g_verbose = 1;
        } else if (!std::strcmp(a, "-h") || !std::strcmp(a, "--help")) {
            Usage();
            return 0;
        } else if (!std::strcmp(a, "--info")) {
            info_only = true;
        } else if (!std::strcmp(a, "--allow-empty")) {
            allow_empty = true;
        } else if (!std::strcmp(a, "-p")) {
            if (++i >= argc) die("-p needs an argument (lz4|fastlz|zlib)");
            pack = argv[i];
        } else if (!std::strcmp(a, "-l")) {
            if (++i >= argc) die("-l needs a comma-separated list argument");
            std::istringstream ss(argv[i]);
            std::string tok;
            while (std::getline(ss, tok, ','))
                if (!tok.empty()) filter.substrs.push_back(tok);
        } else if (!std::strcmp(a, "-L")) {
            if (++i >= argc) die("-L needs a file argument");
            std::FILE *fh = std::fopen(argv[i], "r");
            if (!fh) die("cannot open -L file: %s (%s)", argv[i],
                         std::strerror(errno));
            char line[8192];
            while (std::fgets(line, sizeof(line), fh)) {
                char *p = line;
                while (*p == ' ' || *p == '\t') p++;
                char *e = p + std::strlen(p);
                while (e > p && (e[-1] == '\n' || e[-1] == '\r' ||
                                 e[-1] == ' ' || e[-1] == '\t'))
                    *--e = '\0';
                if (*p && *p != '#') filter.exact.insert(p);
            }
            std::fclose(fh);
            vlog("-L filter: %zu exact paths", filter.exact.size());
        } else if (a[0] == '-' && a[1]) {
            die("unknown option: %s", a);
        } else if (!in_path) {
            in_path = a;
        } else {
            out_arg = a;
        }
    }
    if (!in_path) {
        Usage();
        return 2;
    }

    /* ---- phase 0: read hierarchy + scale ---- */
    FsdbReader rd;
    vlog("opening %s", in_path);
    rd.Open(in_path);

    unsigned int n_real = 0;
    for (const auto &s : rd.signals)
        if (s.is_real) n_real++;
    vinfo("[fsdb2fst] scale: %s (%llu fs per tick)", 
          rd.scale_unit.empty() ? "unknown" : rd.scale_unit.c_str(),
          rd.scale_fs);
    vinfo("[fsdb2fst] signals: %zu (%u real, %u strength-skipped)",
          rd.signals.size(), n_real, rd.n_strength);

    if (info_only) {
        for (size_t i = 0; i < rd.signals.size() && i < 10; i++)
            vinfo("  sample: %s  width=%u%s", rd.signals[i].path.c_str(),
                  rd.signals[i].len, rd.signals[i].is_real ? " (real)" : "");
        if (rd.signals.size() > 10)
            vinfo("  ... and %zu more", rd.signals.size() - 10);
        rd.Close();
        return 0;
    }

    /* ---- select the signals to convert ---- */
    std::vector<Signal *> sel;
    for (auto &s : rd.signals) {
        if (!s.loadable) continue;
        if (!filter.Passes(s.path)) continue;
        sel.push_back(&s);
    }
    if (sel.empty()) die("no convertible signal matches the filter");
    if (sel.size() > 2000000)
        vinfo("[fsdb2fst] warning: %zu signals selected; consider -l/-L "
              "to split the conversion", sel.size());
    vlog("selected %zu signals", sel.size());

    /* ---- output paths ---- */
    std::string out_path = out_arg;
    if (out_path.empty()) {
        std::string in = in_path;
        std::string::size_type dot = in.rfind('.');
        std::string::size_type slash = in.rfind('/');
        if (dot != std::string::npos &&
            (slash == std::string::npos || dot > slash))
            out_path = in.substr(0, dot) + ".fst";
        else
            out_path = in + ".fst";
    }
    std::string tmp_path = out_path + ".tmp.fst";
    std::remove(tmp_path.c_str());

    /* ---- phase 1: build the FST hierarchy ---- */
    auto t0 = std::chrono::steady_clock::now();
    void *wctx = fstWriterCreate(tmp_path.c_str(), 0);
    if (!wctx) die("fstWriterCreate failed for %s", tmp_path.c_str());
    fstWriterSetVersion(wctx, kVersion);
    {
        char datebuf[32];
        std::time_t now = std::time(nullptr);
        std::strftime(datebuf, sizeof(datebuf), "%Y-%m-%d %H:%M:%S",
                      std::localtime(&now));
        fstWriterSetDate(wctx, datebuf);
    }
    fstWriterSetComment(wctx,
        "converted from FSDB by fsdb2fst (wave-mcp); "
        "FsdbReader runtime by Synopsys, not redistributed");
    /* keep the FSDB unit as-is: FST time values are raw counts of 10^exp
     * seconds, FSDB ticks are raw counts of the FSDB unit, so passing the
     * tick through with the same exponent is exact and lossless. */
    {
        char *endp = nullptr;
        /* scale_unit looks like "1ns"/"100fs"; reuse the parsed fs-per-tick
         * to derive the FST exponent (10^exp seconds per tick). */
        int exp10 = 0;
        double num = std::strtod(rd.scale_unit.c_str(), &endp);
        if (endp == rd.scale_unit.c_str()) num = 1.0;
        while (endp && (*endp == ' ' || *endp == '\t')) endp++;
        char unit[8] = {0};
        if (endp) {
            size_t n = 0;
            for (; endp[n] && n + 1 < sizeof(unit); n++)
                unit[n] = static_cast<char>(std::tolower(
                    static_cast<unsigned char>(endp[n])));
        }
        if      (!std::strcmp(unit, "fs")) exp10 = -15;
        else if (!std::strcmp(unit, "ps")) exp10 = -12;
        else if (!std::strcmp(unit, "ns")) exp10 = -9;
        else if (!std::strcmp(unit, "us")) exp10 = -6;
        else if (!std::strcmp(unit, "ms")) exp10 = -3;
        else if (!std::strcmp(unit, "s"))  exp10 =  0;
        else die("unreachable: scale unit \"%s\" parsed earlier but unmapped",
                 rd.scale_unit.c_str());
        /* fold the numeric prefix (1 / 10 / 100) into the exponent; a bare
         * unit equals 1. FSDB scales are 1|10|100 x 10^N fs, so exp10 stays
         * an exact integer in [-21, 0]. */
        if (num == 10.0) exp10 += 1;
        else if (num == 100.0) exp10 += 2;
        else if (num != 1.0)
            die("unsupported FSDB scale prefix %g in \"%s\"", num,
                rd.scale_unit.c_str());
        fstWriterSetTimescale(wctx, exp10);
        vlog("FST timescale exponent: %d (from FSDB scale \"%s\")",
             exp10, rd.scale_unit.c_str());
    }
    if (!std::strcmp(pack, "zlib"))
        fstWriterSetPackType(wctx, FST_WR_PT_ZLIB);
    else if (!std::strcmp(pack, "fastlz"))
        fstWriterSetPackType(wctx, FST_WR_PT_FASTLZ);
    else
        fstWriterSetPackType(wctx, FST_WR_PT_LZ4);

    /* create scopes + vars; dedupe shared idcodes as fst aliases */
    std::map<fsdbVarIdcode, fstHandle> id2fh;      /* idcode -> primary handle */
    std::set<fsdbVarIdcode> alias_ids;             /* ids emitted via primary */
    {
        std::vector<std::string> cur_scope;
        std::set<std::string> seen_path;
        for (Signal *s : sel) {
            /* close/open scopes down to this signal's scope path */
            size_t common = 0;
            while (common < cur_scope.size() && common < s->scope.size() &&
                   cur_scope[common] == s->scope[common])
                common++;
            for (size_t d = cur_scope.size(); d > common; d--)
                fstWriterSetUpscope(wctx);
            for (size_t d = common; d < s->scope.size(); d++) {
                fstWriterSetScope(wctx, FST_ST_VCD_MODULE,
                                  s->scope[d].c_str(), nullptr);
                cur_scope.push_back(s->scope[d]);
            }

            if (!seen_path.insert(s->path).second) {
                s->loadable = false;  /* duplicate path: skip entirely */
                continue;
            }
            fstVarType vt = s->is_real ? FST_VT_VCD_REAL
                                       : MapVarType(s->var_type);
            fstVarDir vd = MapVarDir(s->direction);
            auto it = id2fh.find(s->id);
            if (it != id2fh.end()) {
                /* same idcode as an earlier var: fst alias */
                s->fh = fstWriterCreateVar(wctx, vt, vd, s->len,
                                           s->name.c_str(), it->second);
                alias_ids.insert(s->id);
            } else {
                s->fh = fstWriterCreateVar(wctx, vt, vd, s->len,
                                           s->name.c_str(), 0);
                id2fh[s->id] = s->fh;
            }
            if (!s->fh) die("fstWriterCreateVar failed for %s",
                            s->path.c_str());
        }
        for (size_t d = cur_scope.size(); d > 0; d--)
            fstWriterSetUpscope(wctx);
    }
    vlog("hierarchy written: %zu vars", sel.size());

    /* ---- phase 2: load value data, stream through the merged iterator ---- */
    for (Signal *s : sel) {
        if (s->fh && !alias_ids.count(s->id))
            rd.obj->ffrAddToSignalList(s->id);
    }
    vlog("loading value data for %zu signals (RAM heavy for big designs)...",
         id2fh.size());
    rd.obj->ffrLoadSignals();

    std::vector<fsdbVarIdcode> ids;
    ids.reserve(id2fh.size());
    for (const auto &kv : id2fh) ids.push_back(kv.first);
    ffrTimeBasedVCTrvsHdl thdl =
        rd.obj->ffrCreateTimeBasedVCTrvsHdl(
            static_cast<unsigned int>(ids.size()), ids.data());
    if (!thdl) die("ffrCreateTimeBasedVCTrvsHdl failed");

    /* idcode -> primary signal (id2fh 的 key 对应唯一主变量记录)，避免
     * 遍历热循环里对 sel 做线性扫描 */
    std::unordered_map<fsdbVarIdcode, Signal *> id2sig;
    id2sig.reserve(id2fh.size());
    for (Signal *s : sel) {
        if (s->fh && !alias_ids.count(s->id))
            id2sig.emplace(s->id, s);
    }

    std::vector<unsigned char> buf;
    unsigned long long emitted = 0;
    unsigned long long last_tick = 0;
    bool have_time = false;
    long long rc_fail = 0;

#define EMIT_VEC(s_, vc_)                                            \
    do {                                                             \
        buf.resize((s_)->len);                                       \
        for (unsigned int bi = 0; bi < (s_)->len; bi++) {            \
            unsigned char c = (vc_)[bi];                             \
            switch (c) {                                             \
            case FSDB_BT_VCD_0: buf[bi] = '0'; break;                \
            case FSDB_BT_VCD_1: buf[bi] = '1'; break;                \
            case FSDB_BT_VCD_X: buf[bi] = 'x'; break;                \
            case FSDB_BT_VCD_Z: buf[bi] = 'z'; break;                \
            default:            buf[bi] = 'x'; break;                \
            }                                                        \
        }                                                            \
        fstWriterEmitValueChange(wctx, (s_)->fh, buf.data());        \
    } while (0)

#define EMIT_REAL(s_, vc_)                                           \
    do {                                                             \
        double d = 0.0;                                              \
        if ((s_)->bytes_per_bit == FSDB_BYTES_PER_BIT_4B) {          \
            float f;                                                 \
            std::memcpy(&f, (vc_), sizeof(f));                       \
            d = static_cast<double>(f);                              \
        } else {                                                     \
            std::memcpy(&d, (vc_), sizeof(d));                       \
        }                                                            \
        fstWriterEmitValueChange(wctx, (s_)->fh, &d);                \
    } while (0)

    while (FSDB_RC_SUCCESS == thdl->ffrGotoNextVC()) {
        fsdbVarIdcode idc;
        fsdbXTag xtag;
        byte_T *vc = nullptr;
        fsdbSeqNum seq;
        if (FSDB_RC_SUCCESS !=
            thdl->ffrGetVarIdcodeXTagVCSeqNum(&idc, &xtag, &vc, &seq)) {
            rc_fail++;
            continue;
        }
        if (!vc) continue;
        /* fsdbXTag is layout-compatible with fsdbTag64 in the 64-bit API
         * (TraceWeave relies on the same); assert at compile time. */
        static_assert(sizeof(fsdbXTag) == sizeof(fsdbTag64),
                      "unexpected fsdbXTag size; revisit the tag conversion");
        fsdbTag64 tag;
        std::memcpy(&tag, &xtag, sizeof(tag));
        unsigned long long t_fs =
            (static_cast<unsigned long long>(tag.H) << 32) | tag.L;  /* raw FSDB tick */

        if (!have_time || t_fs != last_tick) {
            fstWriterEmitTimeChange(wctx, t_fs);
            last_tick = t_fs;
            have_time = true;
        }

        auto it = id2fh.find(idc);
        if (it == id2fh.end() || alias_ids.count(idc)) continue;
        auto sit = id2sig.find(idc);
        if (sit == id2sig.end()) continue;
        Signal *s = sit->second;
        if (!s) continue;

        if (s->is_real) {
            EMIT_REAL(s, vc);
        } else {
            EMIT_VEC(s, vc);
        }
        emitted++;
        if (g_verbose && (emitted & 0xFFFFFFFULL) == 0)
            vlog("... %llu transitions, tick=%llu", emitted, last_tick);
    }

    thdl->ffrFree();
    rd.obj->ffrUnloadSignals();
    rd.obj->ffrClose();
    rd.obj = nullptr;

    if (emitted == 0 && !allow_empty)
        die("no value data was loaded from the FSDB (0 transitions); the "
            "file may be truncated, or this FsdbReader version needs a "
            "different load path. Use --allow-empty to keep the "
            "hierarchy-only output.");
    if (rc_fail)
        vlog("warning: %lld records could not be read (ffrGetVarIdcodeXTagVCSeqNum failed)",
             rc_fail);

    fstWriterClose(wctx);
    wctx = nullptr;

    if (std::rename(tmp_path.c_str(), out_path.c_str()) != 0)
        die("cannot move %s to %s (%s)", tmp_path.c_str(), out_path.c_str(),
            std::strerror(errno));

    auto t1 = std::chrono::steady_clock::now();
    double sec =
        std::chrono::duration<double>(t1 - t0).count();
    vinfo("[fsdb2fst] done: %s -> %s", in_path, out_path.c_str());
    vinfo("[fsdb2fst] %zu vars, %llu transitions, %.1f s", sel.size(),
          emitted, sec);
    return 0;
}
