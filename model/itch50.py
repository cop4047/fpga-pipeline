"""Golden model for the ITCH 5.0 / MoldUDP64 version of mdpipe.

Rules are written up in docs/ITCH50.md; layouts follow Nasdaq's TotalView-ITCH
5.0 and MoldUDP64 specifications.  This module is the single source of truth for
the C++ baseline (baseline/cpu_itch50.cpp) and, later, the RTL testbench.

Frame:  Ethernet 14 | IPv4 20 | UDP 8 | MoldUDP64 20 | blocks
Block:  length[2] + message[length]            (big-endian, packed back to back)
"""
from __future__ import annotations

import random
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import mdp_model as L  # legacy ITCH-lite model: Cfg, Tob, order-frame builder

# ----------------------------------------------------------------- parameters
SYM_W = 2
N_SYM = 1 << SYM_W
LVL_W = 6
N_LVL = 1 << LVL_W
ORD_AW = 10
N_ORD = 1 << ORD_AW

MOLD_OFF = 42          # MoldUDP64 header
BLOCKS_OFF = 62        # first message block
MIN_BLOCK = 12         # shortest real ITCH message; shorter blocks end the packet (counted as trunc)
EOS_COUNT = 0xFFFF

SIDE_B, SIDE_S = 0x42, 0x53
# message length by type (known types only; everything else is "ignored")
MSG_LEN = {ord("A"): 36, ord("F"): 40, ord("E"): 31, ord("C"): 36,
           ord("X"): 23, ord("D"): 19, ord("U"): 35}
# lengths used when generating messages the book ignores
IGNORED_LEN = {ord("S"): 12, ord("R"): 39, ord("H"): 25, ord("Y"): 20, ord("P"): 44,
               ord("Q"): 40, ord("B"): 19, ord("I"): 50, ord("N"): 20}

# drop reasons (4-bit code on the RTL drop port)
R_BAD_LEN, R_BAD_SIDE, R_NOT_WATCHED, R_BAD_PRICE = 1, 2, 3, 4
R_BAD_QTY, R_COLLISION, R_DUP_REF, R_UNK_REF = 5, 6, 7, 8
REASON_NAMES = {1: "BAD_LEN", 2: "BAD_SIDE", 3: "NOT_WATCHED", 4: "BAD_PRICE",
                5: "BAD_QTY", 6: "COLLISION", 7: "DUP_REF", 8: "UNK_REF"}

STOCKS = [b"AAPL    ", b"MSFT    ", b"NVDA    ", b"TSLA    "]
OTHER_STOCKS = [b"IBM     ", b"ORCL    ", b"AMD     ", b"INTC    ", b"F       "]


def be(b: bytes) -> int:
    return int.from_bytes(b, "big")


# --------------------------------------------------------------------- config
@dataclass
class Cfg(L.Cfg):
    base_px: int = 1_500_000           # Price(4): $150.0000 = level 0
    buy_px: int = 1_500_000 + 25 * 100  # fire when best ask <= buy_px
    tick: int = 100                    # $0.01
    session: bytes = b"MDPIPE0001"
    watch: List[bytes] = field(default_factory=lambda: list(STOCKS))


# ------------------------------------------------------------ message packers
def _hdr(t: int, locate: int = 0, tracking: int = 0, ts: int = 0) -> bytes:
    return bytes([t]) + struct.pack(">HH", locate, tracking) + (ts & (2**48 - 1)).to_bytes(6, "big")


def pack_add(ref: int, side: int, shs: int, stock: bytes, px: int, *, mpid: bool = False,
             ts: int = 0, locate: int = 0) -> bytes:
    m = _hdr(ord("F") if mpid else ord("A"), locate, 0, ts)
    m += struct.pack(">QBI", ref, side, shs) + stock + struct.pack(">I", px)
    return m + (b"NSDQ" if mpid else b"")


def pack_exec(ref: int, shs: int, *, with_price: bool = False, px: int = 0, ts: int = 0) -> bytes:
    m = _hdr(ord("C") if with_price else ord("E"), 0, 0, ts) + struct.pack(">QIQ", ref, shs, 77)
    return m + (b"Y" + struct.pack(">I", px) if with_price else b"")


def pack_cancel(ref: int, shs: int, ts: int = 0) -> bytes:
    return _hdr(ord("X"), 0, 0, ts) + struct.pack(">QI", ref, shs)


def pack_delete(ref: int, ts: int = 0) -> bytes:
    return _hdr(ord("D"), 0, 0, ts) + struct.pack(">Q", ref)


def pack_replace(old: int, new: int, shs: int, px: int, ts: int = 0) -> bytes:
    return _hdr(ord("U"), 0, 0, ts) + struct.pack(">QQII", old, new, shs, px)


def pack_other(t: int, n: Optional[int] = None, ts: int = 0) -> bytes:
    n = IGNORED_LEN[t] if n is None else n
    return (_hdr(t, 0, 0, ts) + bytes(max(0, n - 11)))[:n]


# ------------------------------------------------------------- frame builder
def build_frame(msgs: List[bytes], cfg: Cfg, seq: int, *, count: Optional[int] = None,
                session: Optional[bytes] = None, ethertype: int = 0x0800, ihl: int = 0x45,
                proto: int = 17, dport: Optional[int] = None) -> bytes:
    """Ethernet+IPv4+UDP+MoldUDP64 frame; `count` overrides the header count."""
    n = len(msgs) if count is None else count
    sess = cfg.session if session is None else session
    payload = sess + struct.pack(">QH", seq, n)
    payload += b"".join(struct.pack(">H", len(m)) + m for m in msgs)
    udp_len = 8 + len(payload)
    ip_len = 20 + udp_len
    dp = cfg.md_dport if dport is None else dport
    f = struct.pack(">6s6sH", (0x01005E000001).to_bytes(6, "big"),
                    (0x020000000099).to_bytes(6, "big"), ethertype)
    f += struct.pack(">BBHHHBBHII", ihl, 0, ip_len, 0, 0x4000, 64, proto, 0, 0x0A000063, 0xEFC00001)
    f += struct.pack(">HHHH", 50000, dp, udp_len, 0)
    f += payload
    if len(f) < 60:
        f += bytes(60 - len(f))
    return f


# ----------------------------------------------------------------- the model
class Model:
    def __init__(self, cfg: Cfg):
        self.cfg = cfg
        self.tbl: Dict[int, list] = {}              # slot -> [ref, sym, side(0=B,1=S), lvl, rem]
        self.lvl: Dict[Tuple[int, int, int], int] = {}
        self.fired = [False] * N_SYM
        self.cid = 1
        self.expected: Optional[int] = None
        self.emitted: List[bytes] = []              # messages handed to the book, in order
        self.tobs: List[L.Tob] = []
        self.drops: List[int] = []
        self.orders_tx: List[bytes] = []
        self.st = dict(frm_good=0, frm_bad=0, msgs=0, ignored=0, gaps=0, dups=0,
                       heartbeats=0, eos=0, trunc=0)

    # ---- packet level ------------------------------------------------------
    def process_frame(self, f: bytes) -> None:
        n = len(f)
        c = self.cfg
        if n < BLOCKS_OFF:
            self.st["frm_bad"] += 1
            return
        ok = (f[12:14] == b"\x08\x00" and f[14] == 0x45 and f[23] == 17
              and be(f[36:38]) == c.md_dport and f[42:52] == c.session)
        if not ok:
            self.st["frm_bad"] += 1
            return
        self.st["frm_good"] += 1
        seq, count = be(f[52:60]), be(f[60:62])
        if count == EOS_COUNT or count == 0:
            self.st["eos" if count == EOS_COUNT else "heartbeats"] += 1
            if self.expected is None:
                self.expected = seq
            elif seq > self.expected:
                self.st["gaps"] += 1
                self.expected = seq
            return
        if self.expected is None:
            self.expected = seq
        if seq > self.expected:
            self.st["gaps"] += 1
            self.expected = seq
        pos = BLOCKS_OFF
        for k in range(count):
            if pos + 2 > n:
                self.st["trunc"] += 1
                return
            ln = be(f[pos:pos + 2])
            if ln < MIN_BLOCK or pos + 2 + ln > n:
                self.st["trunc"] += 1
                return
            m = f[pos + 2:pos + 2 + ln]
            pos += 2 + ln
            if seq + k < self.expected:
                self.st["dups"] += 1
                continue
            self.expected = seq + k + 1
            self.st["msgs"] += 1
            self.emitted.append(m)
            self._message(m)

    # ---- message level -----------------------------------------------------
    def _drop(self, r: int) -> None:
        self.drops.append(r)

    def _level(self, px: int) -> Optional[int]:
        c = self.cfg
        if px < c.base_px:
            return None
        d = px - c.base_px
        if d % c.tick:
            return None
        lv = d // c.tick
        return lv if lv < N_LVL else None

    def _lookup(self, ref: int):
        e = self.tbl.get(ref & (N_ORD - 1))
        return e if (e is not None and e[0] == ref) else None

    def _message(self, m: bytes) -> None:
        t = m[0]
        if t not in MSG_LEN:
            self.st["ignored"] += 1
            return
        if len(m) != MSG_LEN[t]:
            return self._drop(R_BAD_LEN)
        ch = chr(t)
        if ch in "AF":
            ref, side, shs = be(m[11:19]), m[19], be(m[20:24])
            stock, px = m[24:32], be(m[32:36])
            if side not in (SIDE_B, SIDE_S):
                return self._drop(R_BAD_SIDE)
            if stock not in self.cfg.watch:
                return self._drop(R_NOT_WATCHED)
            lv = self._level(px)
            if lv is None:
                return self._drop(R_BAD_PRICE)
            if shs == 0:
                return self._drop(R_BAD_QTY)
            occ = self.tbl.get(ref & (N_ORD - 1))
            if occ is not None:
                return self._drop(R_DUP_REF if occ[0] == ref else R_COLLISION)
            sym, sd = self.cfg.watch.index(stock), (1 if side == SIDE_S else 0)
            self.tbl[ref & (N_ORD - 1)] = [ref, sym, sd, lv, shs]
            k = (sym, sd, lv)
            self.lvl[k] = (self.lvl.get(k, 0) + shs) & 0xFFFFFFFF
            return self._post_update(sym)
        if ch in "EC" or ch == "X":
            ref, shs = be(m[11:19]), be(m[19:23])
            if shs == 0:
                return self._drop(R_BAD_QTY)
            return self._reduce(ref, shs)
        if ch == "D":
            return self._reduce(be(m[11:19]), None)
        # 'U' replace
        old, new, shs, px = be(m[11:19]), be(m[19:27]), be(m[27:31]), be(m[31:35])
        lv = self._level(px)
        if lv is None:
            return self._drop(R_BAD_PRICE)
        if shs == 0:
            return self._drop(R_BAD_QTY)
        e = self._lookup(old)
        if e is None:
            return self._drop(R_UNK_REF)
        so, sn = old & (N_ORD - 1), new & (N_ORD - 1)
        occ = self.tbl.get(sn)
        if occ is not None and sn != so:
            return self._drop(R_DUP_REF if occ[0] == new else R_COLLISION)
        _, sym, sd, olv, rem = e
        ok = (sym, sd, olv)
        self.lvl[ok] = (self.lvl[ok] - rem) & 0xFFFFFFFF
        del self.tbl[so]
        self.tbl[sn] = [new, sym, sd, lv, shs]
        nk = (sym, sd, lv)
        self.lvl[nk] = (self.lvl.get(nk, 0) + shs) & 0xFFFFFFFF
        self._post_update(sym)

    def _reduce(self, ref: int, shs: Optional[int]) -> None:
        e = self._lookup(ref)
        if e is None:
            return self._drop(R_UNK_REF)
        _, sym, sd, lv, rem = e
        delta = rem if (shs is None or shs >= rem) else shs
        k = (sym, sd, lv)
        self.lvl[k] = (self.lvl[k] - delta) & 0xFFFFFFFF
        if rem - delta == 0:
            del self.tbl[ref & (N_ORD - 1)]
        else:
            e[4] = rem - delta
        self._post_update(sym)

    def _best(self, sym: int):
        c = self.cfg
        bids = [l for (s, sd, l), q in self.lvl.items() if s == sym and sd == 0 and q]
        asks = [l for (s, sd, l), q in self.lvl.items() if s == sym and sd == 1 and q]
        bid = (1, c.base_px + max(bids) * c.tick) if bids else (0, 0)
        ask = (1, c.base_px + min(asks) * c.tick) if asks else (0, 0)
        return bid, ask

    def _post_update(self, sym: int) -> None:
        (bv, bp), (av, ap) = self._best(sym)
        self.tobs.append(L.Tob(sym, bv, bp, av, ap))
        cond = bool(self.cfg.en and av and ap <= self.cfg.buy_px)
        if cond and not self.fired[sym]:
            self.orders_tx.append(L.build_order_frame(self.cfg, sym, ap, self.cfg.qty, self.cid))
            self.cid += 1
            self.fired[sym] = True
        elif not cond:
            self.fired[sym] = False


# ------------------------------------------------------------------ generator
def gen_traffic(rng: random.Random, cfg: Cfg, nframes: int, max_msgs: int = 10) -> List[bytes]:
    """Constrained-random MoldUDP64/ITCH stream.

    A private model tracks the exact book so the generator can target live
    orders, delete the best ask to flip the trigger off and on again, and
    produce every kind of anomaly: wrong lengths, bad sides, unwatched stocks,
    off-tick and out-of-window prices, zero shares, duplicate and colliding
    refs, unknown refs, ignored message types, sequence gaps, duplicate and
    overlapping packets, heartbeats, end-of-session, and damaged headers.
    """
    gm = Model(cfg)
    base, tick = cfg.base_px, cfg.tick
    thr = (cfg.buy_px - base) // tick
    st = {"ref": 10**12, "ts": 34_200_000_000_000, "seq": 1000}
    frames: List[bytes] = []
    last_data: List[Optional[bytes]] = [None]
    unwatched_refs: List[int] = []

    def ts() -> int:
        st["ts"] += rng.randint(50, 5000)
        return st["ts"]

    def fresh_ref() -> int:
        st["ref"] += 1
        return st["ref"]

    def px_of(lvl: int) -> int:
        return base + lvl * tick

    def add(side: int, lo: int, hi: int, stock: Optional[bytes] = None) -> bytes:
        stock = stock or rng.choice(cfg.watch)
        lvl = rng.randint(max(lo, 0), min(hi, N_LVL - 1))
        return pack_add(fresh_ref(), side, rng.randint(1, 5000), stock, px_of(lvl),
                        mpid=rng.random() < 0.2, ts=ts())

    def live_refs() -> List[int]:
        return [e[0] for e in gm.tbl.values()]

    def best_ask_ref(sym: int) -> Optional[int]:
        cand = [(e[3], e[0]) for e in gm.tbl.values() if e[1] == sym and e[2] == 1]
        return min(cand)[1] if cand else None

    def bad_msg() -> bytes:
        kind = rng.randrange(11)
        if kind == 0:                                             # wrong length for a known type
            t = rng.choice(list(MSG_LEN))
            return pack_other(t, MSG_LEN[t] + rng.choice((-3, -1, 1, 5)), ts())
        if kind == 1:
            return pack_add(fresh_ref(), 0x58, 10, rng.choice(cfg.watch), px_of(5), ts=ts())
        if kind == 2:                                             # off-tick / below / beyond window
            p = rng.choice((px_of(7) + 1, px_of(7) + tick - 1, base - tick, px_of(N_LVL), 0, 0xFFFFFFFF))
            return pack_add(fresh_ref(), SIDE_S, 10, rng.choice(cfg.watch), p, ts=ts())
        if kind == 3:
            return pack_add(fresh_ref(), SIDE_B, 0, rng.choice(cfg.watch), px_of(5), ts=ts())
        if kind == 4:                                             # duplicate ref
            lr = live_refs()
            return pack_add(rng.choice(lr) if lr else fresh_ref(), SIDE_B, 5,
                            rng.choice(cfg.watch), px_of(9), ts=ts())
        if kind == 5:                                             # same slot, different ref
            lr = live_refs()
            r = (rng.choice(lr) if lr else 7) + N_ORD * rng.randint(1, 3)
            return pack_add(r, SIDE_B, 5, rng.choice(cfg.watch), px_of(9), ts=ts())
        if kind == 6:                                             # unknown refs
            r = rng.choice([10**11 + rng.randrange(10**6), (unwatched_refs or [123])[-1]])
            return rng.choice((pack_delete(r, ts()), pack_exec(r, 5, ts=ts()),
                               pack_cancel(r, 5, ts()), pack_replace(r, fresh_ref(), 5, px_of(4), ts())))
        if kind == 7:                                             # zero shares
            lr = live_refs()
            r = rng.choice(lr) if lr else 5
            return rng.choice((pack_exec(r, 0, ts=ts()), pack_cancel(r, 0, ts()),
                               pack_replace(r, fresh_ref(), 0, px_of(4), ts())))
        if kind == 8:                                             # replace with bad price
            lr = live_refs()
            return pack_replace(rng.choice(lr) if lr else 5, fresh_ref(), 5, px_of(N_LVL + 3), ts())
        if kind == 9:                                             # replace onto an occupied slot
            lr = live_refs()
            if len(lr) >= 2:
                a, b = rng.sample(lr, 2)
                return pack_replace(a, b, 5, px_of(6), ts())
            return pack_other(ord("S"), ts=ts())
        return pack_add(fresh_ref(), SIDE_S, 10, rng.choice(OTHER_STOCKS), px_of(5), ts=ts())

    def pick_msg() -> bytes:
        # Keep the live-order count bounded (like a real feed, orders leave the
        # book); otherwise the 1024-slot table fills and everything collides.
        if len(gm.tbl) > 100 and rng.random() < 0.8:
            lr = live_refs()
            ref = rng.choice(lr)
            rem = gm.tbl[ref & (N_ORD - 1)][4]
            return rng.choice((pack_delete(ref, ts()), pack_exec(ref, rem, ts=ts()),
                               pack_cancel(ref, max(rem // 2, 1), ts())))
        r = rng.random()
        if r < 0.07:
            return pack_other(rng.choice(list(IGNORED_LEN)), ts=ts())
        if r < 0.13:
            return bad_msg()
        if r < 0.22:                                              # unwatched stock add
            m = add(rng.choice((SIDE_B, SIDE_S)), 0, 40, rng.choice(OTHER_STOCKS))
            unwatched_refs.append(be(m[11:19]))
            return m
        r = rng.random()
        if r < 0.26:
            return add(SIDE_S, thr - 10, thr + 25)
        if r < 0.38:
            return add(SIDE_S, 0, thr)                           # re-fire candidate
        if r < 0.52:
            return add(SIDE_B, 0, max(thr - 3, 1))
        if r < 0.72:                                              # delete best ask (flips condition)
            ref = best_ask_ref(rng.randrange(N_SYM))
            if ref is not None:
                return pack_delete(ref, ts())
        if r < 0.74:
            lr = live_refs()
            if lr:
                return pack_delete(rng.choice(lr), ts())
        lr = live_refs()
        if lr:
            ref = rng.choice(lr)
            rem = gm.tbl[ref & (N_ORD - 1)][4]
            q = rng.choice((1, max(rem // 2, 1), rem, rem + 7))
            k = rng.randrange(4)
            if k == 0:
                return pack_exec(ref, q, ts=ts())
            if k == 1:
                return pack_exec(ref, q, with_price=True, px=px_of(rng.randint(0, 63)), ts=ts())
            if k == 2:
                return pack_cancel(ref, q, ts())
            return pack_replace(ref, fresh_ref(), rng.randint(1, 3000), px_of(rng.randint(0, thr + 20)), ts())
        return add(SIDE_B, 0, 20)

    for _ in range(nframes):
        r = rng.random()
        seq = st["seq"]
        if r < 0.03:                                              # heartbeat
            f = build_frame([], cfg, seq)
        elif r < 0.035:                                           # end of session
            f = build_frame([], cfg, seq, count=EOS_COUNT)
        elif r < 0.06 and last_data[0] is not None:               # whole-packet duplicate
            f = last_data[0]
        else:
            msgs: List[bytes] = []
            room = 1452
            for _k in range(rng.randint(1, 40 if rng.random() < 0.03 else max_msgs)):
                m = pick_msg()
                if room < len(m) + 2:
                    break
                room -= len(m) + 2
                msgs.append(m)
            kw = {}
            if rng.random() < 0.03:                               # lost messages -> gap
                seq += rng.randint(1, 6)
            elif rng.random() < 0.03:                             # overlapping retransmit
                seq = max(1, seq - rng.randint(1, 2))
            if rng.random() < 0.04:                               # damaged header
                kind = rng.randrange(8)
                if kind == 0:
                    kw["ethertype"] = 0x86DD
                elif kind == 1:
                    kw["ihl"] = 0x46
                elif kind == 2:
                    kw["proto"] = 6
                elif kind == 3:
                    kw["dport"] = cfg.md_dport + 1
                elif kind == 4:                                   # session differs in one byte
                    sess = bytearray(cfg.session)
                    sess[rng.choice((0, 5, 6, 9))] ^= 0x01        # high half, its edge, low half
                    kw["session"] = bytes(sess)
                elif kind == 5:
                    kw["count"] = len(msgs) + rng.randint(1, 3)
                elif kind == 6:
                    kw["count"] = rng.randint(0, max(len(msgs) - 1, 0))
            f = build_frame(msgs, cfg, seq, **kw)
            if rng.random() < 0.01:                               # truncated / runt
                f = f[:rng.choice((20, 40, 61, rng.randint(62, max(63, len(f) - 1))))]
            last_data[0] = f
            if len(f) >= BLOCKS_OFF and be(f[60:62]) not in (0, EOS_COUNT):
                st["seq"] = max(st["seq"], seq + be(f[60:62]))
        gm.process_frame(f)
        frames.append(f)
    return frames
