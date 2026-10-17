// cpu_itch50 -- the ITCH 5.0 / MoldUDP64 pipeline as plain, fast C++ for one CPU core.
//
// Implements exactly the rules in docs/ITCH50.md and model/itch50.py:
// Ethernet/IPv4/UDP screening, MoldUDP64 sequence tracking (gaps, duplicates,
// heartbeats, end of session), length-prefixed message blocks at arbitrary byte
// offsets, ITCH 5.0 Add/Add-MPID/Executed/Executed-with-price/Cancel/Delete/
// Replace, a 64-bit-tagged direct-mapped order table, a 64-level book per
// {symbol, side} with an occupancy bit mask, the edge-armed price trigger and
// the exact 60-byte order frame.
//
//   cpu_itch50 --check  vectors_itch/        bit-exact against the Python model
//   cpu_itch50 --bench  vectors_itch/ [--pin N] [--passes N]
//
// Benchmark scope is the same as cpu_baseline.cpp: a frame already in memory
// until the order frame is built in memory. No NIC, kernel, PCIe or TX path.

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#if defined(__x86_64__) || defined(_M_X64)
#include <x86intrin.h>
#define HAVE_TSC 1
#endif
#if defined(__linux__)
#include <sched.h>
#endif

namespace {

constexpr int SYM_W = 2, N_SYM = 1 << SYM_W;
constexpr int LVL_W = 6, N_LVL = 1 << LVL_W;
constexpr int ORD_AW = 10, N_ORD = 1 << ORD_AW;
constexpr uint32_t TICK = 100;  // $0.01 in Price(4); the loader refuses other values
constexpr size_t BLOCKS_OFF = 62, ORDER_FRAME_LEN = 60, MIN_BLOCK = 12;
constexpr uint16_t EOS_COUNT = 0xFFFF;
constexpr uint8_t SIDE_B = 0x42, SIDE_S = 0x53;
enum : uint8_t {
    R_BAD_LEN = 1, R_BAD_SIDE, R_NOT_WATCHED, R_BAD_PRICE, R_BAD_QTY, R_COLLISION, R_DUP_REF, R_UNK_REF
};

struct Cfg {
    uint32_t base_px = 1500000, buy_px = 1502500, qty = 100, tick = 100;
    bool en = true;
    uint16_t md_dport = 26400, sport = 40000, o_dport = 9000;
    uint64_t dst_mac = 0x020000000002ULL, src_mac = 0x020000000001ULL;
    uint32_t src_ip = 0x0A000001, dst_ip = 0x0A000002;
    uint8_t session[10] = {};
    uint64_t watch[N_SYM] = {};

    uint16_t ip_csum() const {
        uint32_t w[] = {0x4500, 0x002C, 0x0000, 0x4000, 0x4011, 0x0000,
                        src_ip >> 16, src_ip & 0xFFFF, dst_ip >> 16, dst_ip & 0xFFFF};
        uint32_t s = 0;
        for (uint32_t x : w) s += x;
        while (s >> 16) s = (s & 0xFFFF) + (s >> 16);
        return static_cast<uint16_t>(~s & 0xFFFF);
    }
};

inline uint16_t be16(const uint8_t* p) { return static_cast<uint16_t>((p[0] << 8) | p[1]); }
inline uint32_t be32(const uint8_t* p) {
    return (uint32_t(p[0]) << 24) | (uint32_t(p[1]) << 16) | (uint32_t(p[2]) << 8) | p[3];
}
inline uint64_t be64(const uint8_t* p) { return (uint64_t(be32(p)) << 32) | be32(p + 4); }
inline void put16(uint8_t* p, uint16_t v) { p[0] = v >> 8; p[1] = v & 0xFF; }
inline void put32(uint8_t* p, uint32_t v) { put16(p, v >> 16); put16(p + 2, v & 0xFFFF); }

struct Slot {
    uint64_t ref;
    uint32_t rem;
    uint8_t valid, sym, side, lvl;
};

struct Counters {
    uint32_t frm_good = 0, frm_bad = 0, msgs = 0, ignored = 0, gaps = 0, dups = 0, heartbeats = 0,
             eos = 0, trunc = 0, drops = 0, orders = 0;
};

template <class Sink>
class Pipeline {
public:
    explicit Pipeline(const Cfg& c) : c_(c) {
        std::memset(tbl_, 0, sizeof tbl_);
        std::memset(lvl_, 0, sizeof lvl_);
        std::memset(mask_, 0, sizeof mask_);
        std::memset(fired_, 0, sizeof fired_);
        uint8_t* f = tmpl_;
        std::memset(f, 0, ORDER_FRAME_LEN);
        for (int i = 0; i < 6; i++) f[i] = (c_.dst_mac >> (8 * (5 - i))) & 0xFF;
        for (int i = 0; i < 6; i++) f[6 + i] = (c_.src_mac >> (8 * (5 - i))) & 0xFF;
        put16(f + 12, 0x0800);
        f[14] = 0x45; f[15] = 0; put16(f + 16, 44); put16(f + 18, 0); put16(f + 20, 0x4000);
        f[22] = 64; f[23] = 17; put16(f + 24, c_.ip_csum());
        put32(f + 26, c_.src_ip); put32(f + 30, c_.dst_ip);
        put16(f + 34, c_.sport); put16(f + 36, c_.o_dport); put16(f + 38, 24); put16(f + 40, 0);
        f[42] = 'O'; f[43] = 'B';
    }

    // `n` is the true frame length (no padding assumed).
    void frame(const uint8_t* f, size_t n, Sink& s) {
        if (n < BLOCKS_OFF) { k.frm_bad++; return; }
        const bool ok = f[12] == 0x08 && f[13] == 0x00 && f[14] == 0x45 && f[23] == 17 &&
                        be16(f + 36) == c_.md_dport && std::memcmp(f + 42, c_.session, 10) == 0;
        if (!ok) { k.frm_bad++; return; }
        k.frm_good++;
        const uint64_t seq = be64(f + 52);
        const uint16_t count = be16(f + 60);
        if (count == EOS_COUNT || count == 0) {
            (count == EOS_COUNT ? k.eos : k.heartbeats)++;
            if (!has_exp_) { expected_ = seq; has_exp_ = true; }
            else if (seq > expected_) { k.gaps++; expected_ = seq; }
            return;
        }
        if (!has_exp_) { expected_ = seq; has_exp_ = true; }
        if (seq > expected_) { k.gaps++; expected_ = seq; }
        size_t pos = BLOCKS_OFF;
        for (uint32_t i = 0; i < count; i++) {
            if (pos + 2 > n) { k.trunc++; return; }
            const size_t ln = be16(f + pos);
            if (ln < MIN_BLOCK || pos + 2 + ln > n) { k.trunc++; return; }
            const uint8_t* m = f + pos + 2;
            pos += 2 + ln;
            if (seq + i < expected_) { k.dups++; continue; }
            expected_ = seq + i + 1;
            k.msgs++;
            message(m, ln, s);
        }
    }

    Counters k;

private:
    void drop(uint8_t r, Sink& s) { k.drops++; s.on_drop(r); }

    // level of a price, or -1
    int level_of(uint32_t px) const {
        if (px < c_.base_px) return -1;
        const uint32_t d = px - c_.base_px;
        if (d % TICK) return -1;
        const uint32_t lv = d / TICK;
        return lv < uint32_t(N_LVL) ? int(lv) : -1;
    }

    void set_mask(unsigned sym, unsigned sd, unsigned lv) {
        const uint64_t bit = 1ULL << lv;
        mask_[sym][sd] = lvl_[sym][sd][lv] ? (mask_[sym][sd] | bit) : (mask_[sym][sd] & ~bit);
    }

    void message(const uint8_t* m, size_t ln, Sink& s) {
        const uint8_t t = m[0];
        size_t want;
        switch (t) {
            case 'A': want = 36; break;
            case 'F': want = 40; break;
            case 'E': want = 31; break;
            case 'C': want = 36; break;
            case 'X': want = 23; break;
            case 'D': want = 19; break;
            case 'U': want = 35; break;
            default: k.ignored++; return;
        }
        if (ln != want) return drop(R_BAD_LEN, s);

        if (t == 'A' || t == 'F') {
            const uint64_t ref = be64(m + 11);
            const uint8_t side = m[19];
            const uint32_t shs = be32(m + 20), px = be32(m + 32);
            const uint64_t stock = be64(m + 24);
            if (side != SIDE_B && side != SIDE_S) return drop(R_BAD_SIDE, s);
            int sym = -1;
            for (int i = 0; i < N_SYM; i++) if (c_.watch[i] == stock) { sym = i; break; }
            if (sym < 0) return drop(R_NOT_WATCHED, s);
            const int lv = level_of(px);
            if (lv < 0) return drop(R_BAD_PRICE, s);
            if (shs == 0) return drop(R_BAD_QTY, s);
            Slot& sl = tbl_[ref & (N_ORD - 1)];
            if (sl.valid) return drop(sl.ref == ref ? R_DUP_REF : R_COLLISION, s);
            const uint8_t sd = side == SIDE_S ? 1 : 0;
            sl = Slot{ref, shs, 1, uint8_t(sym), sd, uint8_t(lv)};
            lvl_[sym][sd][lv] += shs;
            set_mask(sym, sd, lv);
            return post_update(uint8_t(sym), s);
        }
        if (t == 'E' || t == 'C' || t == 'X') {
            const uint32_t shs = be32(m + 19);
            if (shs == 0) return drop(R_BAD_QTY, s);
            return reduce(be64(m + 11), shs, false, s);
        }
        if (t == 'D') return reduce(be64(m + 11), 0, true, s);

        // 'U' replace
        const uint64_t oldr = be64(m + 11), newr = be64(m + 19);
        const uint32_t shs = be32(m + 27), px = be32(m + 31);
        const int lv = level_of(px);
        if (lv < 0) return drop(R_BAD_PRICE, s);
        if (shs == 0) return drop(R_BAD_QTY, s);
        const size_t so = oldr & (N_ORD - 1), sn = newr & (N_ORD - 1);
        Slot& o = tbl_[so];
        if (!(o.valid && o.ref == oldr)) return drop(R_UNK_REF, s);
        const Slot& occ = tbl_[sn];
        if (occ.valid && sn != so) return drop(occ.ref == newr ? R_DUP_REF : R_COLLISION, s);
        const uint8_t sym = o.sym, sd = o.side, olv = o.lvl;
        lvl_[sym][sd][olv] -= o.rem;
        set_mask(sym, sd, olv);
        o.valid = 0;
        tbl_[sn] = Slot{newr, shs, 1, sym, sd, uint8_t(lv)};
        lvl_[sym][sd][lv] += shs;
        set_mask(sym, sd, lv);
        post_update(sym, s);
    }

    void reduce(uint64_t ref, uint32_t shs, bool all, Sink& s) {
        Slot& o = tbl_[ref & (N_ORD - 1)];
        if (!(o.valid && o.ref == ref)) return drop(R_UNK_REF, s);
        const uint32_t delta = (all || shs >= o.rem) ? o.rem : shs;
        lvl_[o.sym][o.side][o.lvl] -= delta;
        set_mask(o.sym, o.side, o.lvl);
        o.rem -= delta;
        if (o.rem == 0) o.valid = 0;
        post_update(o.sym, s);
    }

    void post_update(uint8_t sym, Sink& s) {
        const uint64_t bm = mask_[sym][0], am = mask_[sym][1];
        const bool bv = bm != 0, av = am != 0;
        const uint32_t bp = bv ? c_.base_px + (63 - __builtin_clzll(bm)) * TICK : 0;
        const uint32_t ap = av ? c_.base_px + __builtin_ctzll(am) * TICK : 0;
        s.on_tob(sym, bv, bp, av, ap);
        const bool cond = c_.en && av && ap <= c_.buy_px;
        if (cond && !fired_[sym]) {
            uint8_t out[ORDER_FRAME_LEN];
            std::memcpy(out, tmpl_, ORDER_FRAME_LEN);
            put16(out + 44, sym);
            put32(out + 46, cid_++);
            put32(out + 50, ap);
            put32(out + 54, c_.qty);
            fired_[sym] = true;
            k.orders++;
            s.on_order(out);
        } else if (!cond) {
            fired_[sym] = false;
        }
    }

    Cfg c_;
    Slot tbl_[N_ORD];
    uint32_t lvl_[N_SYM][2][N_LVL];
    uint64_t mask_[N_SYM][2];
    bool fired_[N_SYM];
    uint32_t cid_ = 1;
    uint64_t expected_ = 0;
    bool has_exp_ = false;
    uint8_t tmpl_[ORDER_FRAME_LEN];
};

// ---------------------------------------------------------------- sinks
struct CheckSink {
    std::vector<std::string> tob, drop, ord;
    void on_tob(unsigned sym, bool bv, uint32_t bp, bool av, uint32_t ap) {
        char b[96];
        std::snprintf(b, sizeof b, "TOB %u %d %u %d %u", sym, bv, bp, av, ap);
        tob.emplace_back(b);
    }
    void on_drop(unsigned r) { drop.emplace_back("DROP " + std::to_string(r)); }
    void on_order(const uint8_t* f) {
        static const char* hx = "0123456789abcdef";
        std::string s = "ORD ";
        for (size_t i = 0; i < ORDER_FRAME_LEN; i++) { s += hx[f[i] >> 4]; s += hx[f[i] & 15]; }
        ord.push_back(std::move(s));
    }
};

struct BenchSink {
    uint64_t sum = 0;
    inline void on_tob(unsigned sym, bool bv, uint32_t bp, bool av, uint32_t ap) { sum += sym + bv + bp + av + ap; }
    inline void on_drop(unsigned r) { sum += r; }
    inline void on_order(const uint8_t* f) {
        uint64_t x; std::memcpy(&x, f + 44, 8); sum ^= x;
        std::memcpy(&x, f + 52, 8); sum ^= x;
    }
};

// ---------------------------------------------------------------- vectors
struct Vectors {
    Cfg cfg;
    std::vector<uint8_t> arena;
    std::vector<std::pair<size_t, size_t>> fr;  // (offset, true length)
    std::vector<std::string> tob, drop, ord;
    Counters want;
};

int hexval(char c) { return c <= '9' ? c - '0' : (c | 32) - 'a' + 10; }

bool load(const std::string& dir, Vectors& v) {
    std::ifstream fb(dir + "/frames.bin", std::ios::binary);
    if (!fb) { std::fprintf(stderr, "cannot open %s/frames.bin\n", dir.c_str()); return false; }
    uint32_t len;
    std::vector<uint8_t> tmp;
    while (fb.read(reinterpret_cast<char*>(&len), 4)) {
        tmp.resize(len);
        fb.read(reinterpret_cast<char*>(tmp.data()), len);
        v.fr.emplace_back(v.arena.size(), len);
        v.arena.insert(v.arena.end(), tmp.begin(), tmp.end());
    }
    // slack so a speculative read just past a frame can never leave the buffer
    v.arena.insert(v.arena.end(), 64, 0);
    std::ifstream fe(dir + "/expected.txt");
    if (!fe) { std::fprintf(stderr, "cannot open %s/expected.txt\n", dir.c_str()); return false; }
    std::string line;
    while (std::getline(fe, line)) {
        std::istringstream is(line);
        std::string tag;
        is >> tag;
        if (tag == "CFG") {
            std::string k, val;
            is >> k >> val;
            auto num = [&] { return std::strtoull(val.c_str(), nullptr, 10); };
            if (k == "base_px") v.cfg.base_px = num();
            else if (k == "buy_px") v.cfg.buy_px = num();
            else if (k == "qty") v.cfg.qty = num();
            else if (k == "tick") v.cfg.tick = num();
            else if (k == "en") v.cfg.en = num();
            else if (k == "md_dport") v.cfg.md_dport = num();
            else if (k == "dst_mac") v.cfg.dst_mac = num();
            else if (k == "src_mac") v.cfg.src_mac = num();
            else if (k == "src_ip") v.cfg.src_ip = num();
            else if (k == "dst_ip") v.cfg.dst_ip = num();
            else if (k == "sport") v.cfg.sport = num();
            else if (k == "o_dport") v.cfg.o_dport = num();
            else if (k == "session") {
                for (int i = 0; i < 10; i++) v.cfg.session[i] = hexval(val[2 * i]) * 16 + hexval(val[2 * i + 1]);
            } else if (k.rfind("watch", 0) == 0) {
                uint64_t w = 0;
                for (int i = 0; i < 8; i++) w = (w << 8) | uint64_t(hexval(val[2 * i]) * 16 + hexval(val[2 * i + 1]));
                v.cfg.watch[std::atoi(k.c_str() + 5)] = w;
            }
        } else if (tag == "TOB") v.tob.push_back(line);
        else if (tag == "DROP") v.drop.push_back(line);
        else if (tag == "ORD") v.ord.push_back(line);
        else if (tag == "COUNTS") {
            Counters& w = v.want;
            is >> w.frm_good >> w.frm_bad >> w.msgs >> w.ignored >> w.gaps >> w.dups >> w.heartbeats >>
                w.eos >> w.trunc >> w.drops >> w.orders;
        }
    }
    if (v.cfg.tick != TICK) { std::fprintf(stderr, "tick %u unsupported (built for %u)\n", v.cfg.tick, TICK); return false; }
    return true;
}

int check(const Vectors& v) {
    Pipeline<CheckSink> p(v.cfg);
    CheckSink s;
    for (auto [off, len] : v.fr) p.frame(v.arena.data() + off, len, s);
    int bad = 0;
    auto cmp = [&](const char* name, const std::vector<std::string>& got, const std::vector<std::string>& exp) {
        if (got.size() != exp.size()) {
            std::printf("FAIL %s: count cpu=%zu model=%zu\n", name, got.size(), exp.size());
            bad++;
            return;
        }
        for (size_t i = 0; i < got.size(); i++)
            if (got[i] != exp[i]) {
                std::printf("FAIL %s #%zu:\n  cpu   =%s\n  model =%s\n", name, i, got[i].c_str(), exp[i].c_str());
                bad++;
                return;
            }
        std::printf("ok   %-5s %zu events identical\n", name, got.size());
    };
    cmp("TOB", s.tob, v.tob);
    cmp("DROP", s.drop, v.drop);
    cmp("ORD", s.ord, v.ord);
    const Counters &a = p.k, &w = v.want;
    if (a.frm_good != w.frm_good || a.frm_bad != w.frm_bad || a.msgs != w.msgs || a.ignored != w.ignored ||
        a.gaps != w.gaps || a.dups != w.dups || a.heartbeats != w.heartbeats || a.eos != w.eos ||
        a.trunc != w.trunc || a.drops != w.drops || a.orders != w.orders) {
        std::printf("FAIL counters\n  cpu   = %u %u %u %u %u %u %u %u %u %u %u\n  model = %u %u %u %u %u %u %u %u %u %u %u\n",
                    a.frm_good, a.frm_bad, a.msgs, a.ignored, a.gaps, a.dups, a.heartbeats, a.eos, a.trunc,
                    a.drops, a.orders, w.frm_good, w.frm_bad, w.msgs, w.ignored, w.gaps, w.dups, w.heartbeats,
                    w.eos, w.trunc, w.drops, w.orders);
        bad++;
    } else {
        std::printf("ok   counters identical (msgs=%u ignored=%u gaps=%u dups=%u)\n", a.msgs, a.ignored, a.gaps, a.dups);
    }
    std::printf(bad ? "CHECK FAILED\n" : "CHECK PASSED: C++ output is bit-exact with the golden model\n");
    return bad ? 1 : 0;
}

// ---------------------------------------------------------------- benchmark
inline uint64_t now_ticks() {
#ifdef HAVE_TSC
    unsigned aux;
    return __rdtscp(&aux);
#else
    return std::chrono::steady_clock::now().time_since_epoch().count();
#endif
}

double ticks_per_ns() {
#ifdef HAVE_TSC
    using clk = std::chrono::steady_clock;
    auto t0 = clk::now(); uint64_t c0 = now_ticks();
    while (clk::now() - t0 < std::chrono::milliseconds(300)) {}
    auto t1 = clk::now(); uint64_t c1 = now_ticks();
    return double(c1 - c0) / std::chrono::duration<double, std::nano>(t1 - t0).count();
#else
    return 1.0;
#endif
}

double pct(std::vector<double>& v, double p) {
    if (v.empty()) return 0;
    return v[std::min(v.size() - 1, size_t(p / 100.0 * v.size()))];
}

int bench(const Vectors& v, int pin, int passes) {
#if defined(__linux__)
    if (pin >= 0) {
        cpu_set_t set; CPU_ZERO(&set); CPU_SET(pin, &set);
        if (sched_setaffinity(0, sizeof set, &set) != 0) std::perror("sched_setaffinity");
    }
#endif
    const double tpn = ticks_per_ns();
    uint64_t ovh = ~0ULL;
    for (int i = 0; i < 100000; i++) { uint64_t a = now_ticks(), b = now_ticks(); ovh = std::min(ovh, b - a); }

    BenchSink sink;
    auto run_pass = [&] {
        Pipeline<BenchSink> p(v.cfg);
        for (auto [off, len] : v.fr) p.frame(v.arena.data() + off, len, sink);
        return p.k.msgs;
    };
    uint32_t msgs_per_pass = 0;
    for (int i = 0; i < 3; i++) msgs_per_pass = run_pass();

    auto t0 = std::chrono::steady_clock::now();
    for (int i = 0; i < passes; i++) run_pass();
    auto t1 = std::chrono::steady_clock::now();
    const double secs = std::chrono::duration<double>(t1 - t0).count();

    std::vector<double> lat_all, lat_ord;
    for (int i = 0; i < passes; i++) {
        Pipeline<BenchSink> p(v.cfg);
        for (auto [off, len] : v.fr) {
            const uint32_t before = p.k.orders;
            uint64_t a = now_ticks();
            p.frame(v.arena.data() + off, len, sink);
            uint64_t b = now_ticks();
            const double ns = double(b - a - std::min(b - a, ovh)) / tpn;
            lat_all.push_back(ns);
            if (p.k.orders != before) lat_ord.push_back(ns);
        }
    }
    std::sort(lat_all.begin(), lat_all.end());
    std::sort(lat_ord.begin(), lat_ord.end());

    std::printf("frames/pass=%zu  messages/pass=%u  passes=%d  (sink=%llu)\n", v.fr.size(), msgs_per_pass, passes,
                (unsigned long long)sink.sum);
    std::printf("timer: %s, %.3f ticks/ns, overhead %.1f ns (subtracted)\n",
#ifdef HAVE_TSC
                "rdtscp",
#else
                "steady_clock",
#endif
                tpn, ovh / tpn);
    std::printf("throughput: %.2f M msgs/s, %.2f M frames/s  (%.1f ns/msg amortised)\n",
                double(msgs_per_pass) * passes / secs / 1e6, double(v.fr.size()) * passes / secs / 1e6,
                secs * 1e9 / (double(msgs_per_pass) * passes));
    auto row = [&](const char* name, std::vector<double>& x) {
        std::printf("%-26s n=%-8zu p50=%7.1f  p90=%7.1f  p99=%7.1f  p99.9=%7.1f  max=%8.1f ns\n", name, x.size(),
                    pct(x, 50), pct(x, 90), pct(x, 99), pct(x, 99.9), x.empty() ? 0.0 : x.back());
    };
    row("frame -> done (all)", lat_all);
    row("frame -> order built", lat_ord);
    std::printf("\nScope: frame already in cache-warm memory; order built in memory. No NIC, kernel, PCIe or\n"
                "TX path. Multi-message frames: the last column includes every message ahead of the trigger.\n");
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    std::string mode, dir;
    int pin = -1, passes = 50;
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        if (a == "--check" || a == "--bench") mode = a;
        else if (a == "--pin" && i + 1 < argc) pin = std::atoi(argv[++i]);
        else if (a == "--passes" && i + 1 < argc) passes = std::atoi(argv[++i]);
        else dir = a;
    }
    if (mode.empty() || dir.empty()) {
        std::fprintf(stderr, "usage: %s --check|--bench <vectors-dir> [--pin CPU] [--passes N]\n", argv[0]);
        return 2;
    }
    Vectors v;
    if (!load(dir, v)) return 2;
    return mode == "--check" ? check(v) : bench(v, pin, passes);
}
