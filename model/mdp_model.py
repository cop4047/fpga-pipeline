"""Golden reference model for the mdpipe market-data -> order pipeline.

Everything the RTL does is mirrored here at the *transaction* level:
  * Ethernet/IPv4/UDP header screening of incoming frames
  * parsing of ITCH-lite messages (Add / Delete / Execute)
  * per-symbol price-level order book with a bounded price window
  * the price-threshold trigger (edge-armed per symbol)
  * the exact bytes of the outbound order frame

The cocotb testbench compares the RTL against this model event by event, so
any divergence in semantics (drop-reason precedence, clamping, ...) shows up
as a test failure.  Keep the two in lock-step: the rules are written up in
docs/SPEC.md.
"""
from __future__ import annotations

import random
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ----------------------------------------------------------------- parameters
# Must match the parameters of rtl/mdp_top.sv.
SYM_W = 2
N_SYM = 1 << SYM_W
LVL_W = 6
N_LVL = 1 << LVL_W
ORD_AW = 10

# message types / sides (ASCII, as in real ITCH)
T_ADD, T_DEL, T_EXE = 0x41, 0x44, 0x45          # 'A' 'D' 'E'
SIDE_B, SIDE_S = 0x42, 0x53                      # 'B' 'S'

# drop reasons (3-bit code on the RTL drop port)
R_BAD_SYM = 1
R_BAD_PRICE = 2
R_DUP_REF = 3
R_UNK_REF = 4
R_BAD_TYPE = 5
R_BAD_REF = 6
R_BAD_QTY = 7
REASON_NAMES = {1: "BAD_SYM", 2: "BAD_PRICE", 3: "DUP_REF", 4: "UNK_REF",
                5: "BAD_TYPE", 6: "BAD_REF", 7: "BAD_QTY"}

PAYLOAD_OFF = 44      # eth(14) + ip(20) + udp(8) + count(2)
MSG_LEN = 16
ORDER_FRAME_LEN = 60  # 42 hdr + 16 payload + 2 pad (== Ethernet minimum w/o FCS)


# --------------------------------------------------------------------- config
@dataclass
class Cfg:
    base_px: int = 1000          # price of level 0 of the book window
    buy_px: int = 1025           # fire when best ask <= buy_px
    qty: int = 100               # order quantity
    en: int = 1
    md_dport: int = 26400        # market-data UDP destination port we accept
    # outbound order-gateway framing
    dst_mac: int = 0x02_00_00_00_00_02
    src_mac: int = 0x02_00_00_00_00_01
    src_ip: int = 0x0A_00_00_01  # 10.0.0.1
    dst_ip: int = 0x0A_00_00_02  # 10.0.0.2
    sport: int = 40000
    o_dport: int = 9000

    def ip_csum(self) -> int:
        """IPv4 header checksum of the (constant) outbound header."""
        words = [0x4500, 0x002C, 0x0000, 0x4000, 0x4011, 0x0000,
                 self.src_ip >> 16, self.src_ip & 0xFFFF,
                 self.dst_ip >> 16, self.dst_ip & 0xFFFF]
        s = sum(words)
        while s >> 16:
            s = (s & 0xFFFF) + (s >> 16)
        return (~s) & 0xFFFF


# ------------------------------------------------------------ frame builders
@dataclass
class Msg:
    type: int
    side: int
    sym: int
    ref: int
    price: int
    shares: int

    def pack(self) -> bytes:
        return struct.pack(">BBHIII", self.type, self.side, self.sym,
                           self.ref, self.price, self.shares)


def build_mkt_frame(msgs: List[Msg], cfg: Cfg, *, ethertype: int = 0x0800,
                    ihl: int = 0x45, proto: int = 17,
                    dport: Optional[int] = None,
                    count: Optional[int] = None) -> bytes:
    """Ethernet+IPv4+UDP frame carrying `count` ITCH-lite messages."""
    n = len(msgs) if count is None else count
    payload = struct.pack(">H", n) + b"".join(m.pack() for m in msgs)
    udp_len = 8 + len(payload)
    ip_len = 20 + udp_len
    dp = cfg.md_dport if dport is None else dport
    f = struct.pack(">6s6sH", (0x01005E000001).to_bytes(6, "big"),
                    (0x020000000099).to_bytes(6, "big"), ethertype)
    f += struct.pack(">BBHHHBBHII", ihl, 0, ip_len, 0, 0x4000, 64, proto, 0,
                     0x0A000063, 0xEFC00001)
    f += struct.pack(">HHHH", 50000, dp, udp_len, 0)
    f += payload
    if len(f) < 60:
        f += bytes(60 - len(f))
    return f


def build_order_frame(cfg: Cfg, sym: int, px: int, qty: int, cid: int) -> bytes:
    f = cfg.dst_mac.to_bytes(6, "big") + cfg.src_mac.to_bytes(6, "big")
    f += struct.pack(">H", 0x0800)
    f += struct.pack(">BBHHHBBH", 0x45, 0, 44, 0, 0x4000, 64, 17, cfg.ip_csum())
    f += struct.pack(">II", cfg.src_ip, cfg.dst_ip)
    f += struct.pack(">HHHH", cfg.sport, cfg.o_dport, 24, 0)
    f += struct.pack(">BBHIII", 0x4F, 0x42, sym, cid, px, qty)   # 'O' 'B'
    f += bytes(2)
    assert len(f) == ORDER_FRAME_LEN
    return f


def frame_to_beats(frame: bytes) -> List[int]:
    """Bytes -> list of 64-bit AXI-Stream beats (byte 0 in tdata[7:0])."""
    frame = frame + bytes(-len(frame) % 8)
    return [int.from_bytes(frame[i:i + 8], "little") for i in range(0, len(frame), 8)]


# ----------------------------------------------------------------- the model
@dataclass
class Tob:
    sym: int
    bid_vld: int
    bid_px: int
    ask_vld: int
    ask_px: int


class Model:
    def __init__(self, cfg: Cfg):
        self.cfg = cfg
        self.orders: Dict[int, List[int]] = {}      # ref -> [sym, side(0=B,1=S), lvl, rem]
        self.lvl: Dict[Tuple[int, int, int], int] = {}
        self.fired = [False] * N_SYM
        self.cid = 1
        # observable event streams (compared against the RTL)
        self.tobs: List[Tob] = []
        self.drops: List[int] = []
        self.orders_tx: List[bytes] = []
        self.frm_good = 0
        self.frm_bad = 0
        self.msgs_seen = 0

    # -- frame level ------------------------------------------------------
    def process_frame(self, frame: bytes) -> None:
        nbeats = (len(frame) + 7) // 8
        fr = frame + bytes(-len(frame) % 8)
        if nbeats < 7:                           # ends before the first payload word
            self.frm_bad += 1
            return
        ok = (fr[12:14] == b"\x08\x00" and fr[14] == 0x45 and fr[23] == 17
              and int.from_bytes(fr[36:38], "big") == self.cfg.md_dport)
        if not ok:
            self.frm_bad += 1
            return
        self.frm_good += 1
        count = int.from_bytes(fr[42:44], "big")
        words = nbeats - 6                       # aligned 64-bit words available
        n = min(count, words // 2)
        for k in range(n):
            b = fr[PAYLOAD_OFF + MSG_LEN * k: PAYLOAD_OFF + MSG_LEN * (k + 1)]
            t, s, sym, ref, px, shs = struct.unpack(">BBHIII", b)
            self.msgs_seen += 1
            self._apply(Msg(t, s, sym, ref, px, shs))

    # -- message level ----------------------------------------------------
    def _drop(self, r: int) -> None:
        self.drops.append(r)

    def _apply(self, m: Msg) -> None:
        base = self.cfg.base_px
        is_add, is_del, is_exe = m.type == T_ADD, m.type == T_DEL, m.type == T_EXE
        if is_add:
            type_ok = m.side in (SIDE_B, SIDE_S)
        else:
            type_ok = is_del or is_exe
        if not type_ok:
            return self._drop(R_BAD_TYPE)
        if m.ref >> ORD_AW:
            return self._drop(R_BAD_REF)
        if is_add:
            if m.sym >> SYM_W:
                return self._drop(R_BAD_SYM)
            if m.price < base or (m.price - base) >= N_LVL:
                return self._drop(R_BAD_PRICE)
        if (is_add or is_exe) and m.shares == 0:
            return self._drop(R_BAD_QTY)

        if is_add:
            if m.ref in self.orders:
                return self._drop(R_DUP_REF)
            side = 1 if m.side == SIDE_S else 0
            lvl = m.price - base
            self.orders[m.ref] = [m.sym, side, lvl, m.shares]
            key = (m.sym, side, lvl)
            self.lvl[key] = (self.lvl.get(key, 0) + m.shares) & 0xFFFFFFFF
            sym = m.sym
        else:
            if m.ref not in self.orders:
                return self._drop(R_UNK_REF)
            sym, side, lvl, rem = self.orders[m.ref]
            delta = rem if (is_del or m.shares >= rem) else m.shares
            key = (sym, side, lvl)
            self.lvl[key] = (self.lvl[key] - delta) & 0xFFFFFFFF
            if rem - delta == 0:
                del self.orders[m.ref]
            else:
                self.orders[m.ref][3] = rem - delta
        self._post_update(sym)

    def _best(self, sym: int):
        bids = [l for (s, sd, l), q in self.lvl.items() if s == sym and sd == 0 and q]
        asks = [l for (s, sd, l), q in self.lvl.items() if s == sym and sd == 1 and q]
        bid = (1, self.cfg.base_px + max(bids)) if bids else (0, 0)
        ask = (1, self.cfg.base_px + min(asks)) if asks else (0, 0)
        return bid, ask

    def _post_update(self, sym: int) -> None:
        (bv, bp), (av, ap) = self._best(sym)
        self.tobs.append(Tob(sym, bv, bp, av, ap))
        cond = bool(self.cfg.en and av and ap <= self.cfg.buy_px)
        if cond and not self.fired[sym]:
            self.orders_tx.append(build_order_frame(self.cfg, sym, ap, self.cfg.qty, self.cid))
            self.cid += 1
            self.fired[sym] = True
        elif not cond:
            self.fired[sym] = False


# ------------------------------------------------------------------ generator
def gen_traffic(rng: random.Random, cfg: Cfg, nframes: int,
                bad_hdr_pct: float = 0.04, bad_msg_pct: float = 0.08,
                max_msgs: int = 4) -> List[bytes]:
    """Constrained-random frame stream.

    The generator drives a private copy of the model so it always knows the
    exact book state: it can target real order refs and, importantly, delete the
    *best ask* to flip the trigger condition off and on again, which is what
    exercises the edge-armed fire / re-arm logic.  A fraction of messages and
    frames are deliberately malformed to hit every drop / reject path.
    """
    gm = Model(cfg)
    base, thr = cfg.base_px, cfg.buy_px - cfg.base_px
    state = {"next_ref": 1}
    frames: List[bytes] = []

    def fresh_ref() -> int:
        for _ in range(64):
            r = state["next_ref"]
            state["next_ref"] = r % ((1 << ORD_AW) - 1) + 1
            if r not in gm.orders:
                return r
        return state["next_ref"]

    def add(side: int, lo: int, hi: int) -> Msg:
        return Msg(T_ADD, side, rng.randrange(N_SYM), fresh_ref(),
                   base + rng.randint(max(lo, 0), min(hi, N_LVL - 1)), rng.randint(1, 5000))

    def best_ask_ref(sym: int) -> Optional[int]:
        cand = [(v[2], r) for r, v in gm.orders.items() if v[0] == sym and v[1] == 1]
        return min(cand)[1] if cand else None

    def random_live() -> Optional[int]:
        return rng.choice(list(gm.orders)) if gm.orders else None

    def bad_msg() -> Msg:
        m = add(SIDE_B, 0, 40)
        kind = rng.randrange(7)
        if kind == 0:
            m.type = rng.choice((0x00, 0x58, 0x5A))
        elif kind == 1:
            m.side = 0x58
        elif kind == 2:
            m.sym = rng.randint(N_SYM, 0xFFFF)
        elif kind == 3:
            m.price = rng.choice((base - 1, base + N_LVL, 0, 0xFFFFFFFF))
        elif kind == 4:
            m.shares = 0
        elif kind == 5:
            m.ref = rng.randint(1 << ORD_AW, 0xFFFFFFFF)
        else:
            m.ref = random_live() or m.ref            # duplicate ref (if any live)
        if rng.random() < 0.15:                       # unknown-ref delete / execute
            m = Msg(rng.choice((T_DEL, T_EXE)), 0, 0, rng.randint(1, (1 << ORD_AW) - 1), 0,
                    rng.choice((0, 5)))
        return m

    def pick_msg() -> Msg:
        r = rng.random()
        if r < bad_msg_pct:
            return bad_msg()
        r = rng.random()
        if r < 0.30:                                            # ask around the threshold
            return add(SIDE_S, thr - 10, thr + 25)
        if r < 0.45:                                            # low ask: re-fire candidate
            return add(SIDE_S, 0, thr)
        if r < 0.62:                                            # bid
            return add(SIDE_B, 0, max(thr - 3, 1))
        if r < 0.82:                                            # delete the best ask (flip cond)
            ref = best_ask_ref(rng.randrange(N_SYM))
            if ref is not None:
                return Msg(T_DEL, 0, 0, ref, 0, 0)
        if r < 0.90:                                            # delete a random live order
            ref = random_live()
            if ref is not None:
                return Msg(T_DEL, 0, 0, ref, 0, 0)
        ref = random_live()
        if ref is not None:                                     # execute (partial or over)
            rem = gm.orders[ref][3]
            return Msg(T_EXE, 0, 0, ref, 0, rng.choice((1, max(rem // 2, 1), rem, rem + 7)))
        return add(SIDE_B, 0, 20)

    for _ in range(nframes):
        msgs = [pick_msg() for _ in range(rng.randint(1, max_msgs))]
        kw = {}
        if rng.random() < bad_hdr_pct:
            kind = rng.randrange(6)
            if kind == 0:
                kw["ethertype"] = 0x86DD
            elif kind == 1:
                kw["ihl"] = 0x46
            elif kind == 2:
                kw["proto"] = 6
            elif kind == 3:
                kw["dport"] = cfg.md_dport + 1
            elif kind == 4:
                kw["count"] = len(msgs) + rng.randint(1, 3)      # count > payload
            else:
                kw["count"] = rng.randint(0, len(msgs) - 1)      # count < payload
        f = build_mkt_frame(msgs, cfg, **kw)
        gm.process_frame(f)
        frames.append(f)
    return frames
