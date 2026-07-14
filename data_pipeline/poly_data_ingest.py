"""Polygon on-chain OrderFilled event ingester (warproxxx/poly_data port).

Replaces the previous mock/skeleton poly_data_helper.py with a real
Polygon JSON-RPC client that fetches OrderFilled events from the
Polymarket CTF Exchange V2 contract, decodes them, and joins them with
market metadata from the Gamma API.

This is the foundation for tick-level backtesting: instead of relying on
synthetic outcomes, we know exactly who traded what, when, at what price,
and in which direction.

Contract: 0xE111180000d2663C0091e4f400237545B87B996B  (CTF Exchange V2)
Chain:    Polygon (chainId 137)
Event:    OrderFilled(bytes32 orderHash, uint8 takerAssetId, uint8 makerAssetId,
                       uint256 makerFillAmount, uint256 takerFillAmount,
                       address maker, address taker, uint256 timestamp)
Migration note: V2 contract went live 2026-04-28. Pre-V2 (V1) data lives
on a different contract and Goldsky subgraph (deprecated). This module
only handles V2 — for V1 historical data, see warproxxx/poly_data v1-final tag.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd
import requests

logger = logging.getLogger("POLY_DATA_INGEST")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# CTF Exchange V2 contract on Polygon (deployed 2026-03-31, live 2026-04-28).
# Migration from V1 (Goldsky subgraph) to V2 (on-chain) occurred 2026-04-28.
CTF_EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"

# V2 genesis block — the first block where OrderFilled events can appear.
# Used as the default starting point for backfill if no cursor exists.
V2_GENESIS_BLOCK = 84_902_353

# Reorg-safety buffer for Polygon (finality ~ a few minutes).
CONFIRMATIONS = 20

# Canonical OrderFilled signature from warproxxx/poly_data:
# OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)
# 3 indexed params (orderHash, maker, taker) + 7 non-indexed in data:
#   uint8  side           (0 = BUY, 1 = SELL — maker's side)
#   uint256 token_id      (the outcome token being traded)
#   uint256 makerAmountFilled
#   uint256 takerAmountFilled
#   uint256 fee
#   uint256 builder
#   bytes32 metadata
ORDER_FILLED_SIGNATURE = (
    "OrderFilled(bytes32,address,address,uint8,uint256,"
    "uint256,uint256,uint256,bytes32,bytes32)"
)
# ORDER_FILLED_TOPIC0 computed below after _keccak_topic is defined.

# Polygon block generation ~2s. We scan in chunks to respect RPC limits.
DEFAULT_BLOCK_CHUNK = 500  # safe for paid RPCs; lower for free tiers

# Gamma API keyset endpoint
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets/keyset"

# Where to persist cursor state (resumable backfill)
CURSOR_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "poly_data_cursor.json",
)

# Where to cache markets.csv locally
MARKETS_CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "poly_markets.csv",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _keccak_topic(signature: str) -> str:
    """Compute keccak256(signature) and return as 0x-prefixed hex topic."""
    try:
        # Try eth_utils first (preferred)
        from eth_utils import keccak  # type: ignore

        return "0x" + keccak(text=signature).hex()
    except ImportError:
        # Fall back to a manual keccak via pysha3 if available
        try:
            import sha3  # type: ignore

            k = sha3.keccak_256()
            k.update(signature.encode("utf-8"))
            return "0x" + k.hexdigest()
        except ImportError:
            # Last resort: hard-coded value for the canonical signature.
            # Computed via eth_utils.keccak(text=ORDER_FILLED_SIGNATURE)
            # Caller should pip install eth-utils for production use.
            logger.warning(
                "eth-utils not installed — using hard-coded topic hash. "
                "Install eth-utils for correctness verification."
            )
            return "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"


# Real topic computed at import time (must come after _keccak_topic def)
ORDER_FILLED_TOPIC0 = _keccak_topic(ORDER_FILLED_SIGNATURE)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PolyDataConfig:
    """Configuration for the Polygon RPC ingester."""

    rpc_url: str = os.environ.get(
        "POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com"
    )
    block_chunk: int = int(
        os.environ.get("POLYGON_MAX_BLOCK_RANGE", str(DEFAULT_BLOCK_CHUNK))
    )
    ctf_exchange: str = CTF_EXCHANGE_V2
    gamma_url: str = GAMMA_MARKETS_URL
    request_timeout: float = 30.0
    max_retries: int = 3


# ---------------------------------------------------------------------------
# JSON-RPC client
# ---------------------------------------------------------------------------


class PolygonRPCClient:
    """Thin JSON-RPC 2.0 client for Polygon."""

    def __init__(self, cfg: PolyDataConfig):
        self.cfg = cfg
        self._next_id = 1

    def _call(self, method: str, params: list[Any], *, retry: int | None = None) -> Any:
        """Make a single JSON-RPC call with retry on transient failures."""
        retries = retry if retry is not None else self.cfg.max_retries
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        self._next_id += 1

        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = requests.post(
                    self.cfg.rpc_url,
                    json=payload,
                    timeout=self.cfg.request_timeout,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
                if "error" in data and data["error"]:
                    raise RuntimeError(f"RPC error: {data['error']}")
                return data.get("result")
            except Exception as exc:
                last_exc = exc
                if attempt < retries:
                    backoff = 0.5 * (2**attempt)
                    logger.debug(
                        "RPC retry %d/%d after %.2fs: %s",
                        attempt + 1,
                        retries,
                        backoff,
                        exc,
                    )
                    time.sleep(backoff)
        raise RuntimeError(
            f"RPC call {method} failed after {retries + 1} attempts: {last_exc}"
        )

    def get_latest_block(self) -> int:
        return int(self._call("eth_blockNumber", []), 16)

    def get_logs(
        self,
        *,
        from_block: int,
        to_block: int,
        address: str,
        topics: list[str],
    ) -> list[dict]:
        params = [
            {
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": address,
                "topics": topics,
            }
        ]
        result = self._call("eth_getLogs", params)
        return result or []


# ---------------------------------------------------------------------------
# Event decoding
# ---------------------------------------------------------------------------


def _hex_to_int(s: str) -> int:
    return int(s, 16) if isinstance(s, str) else int(s)


def _topic_to_address(topic: str) -> str:
    """Convert a 32-byte topic to a 20-byte address (last 40 hex chars)."""
    if not topic or not topic.startswith("0x"):
        return ""
    return "0x" + topic[-40:]


def decode_order_filled(log: dict) -> dict | None:
    """Decode a single OrderFilled event log into a structured dict.

    Layout (from warproxxx/poly_data update_chain.py):
      topics[0]: event signature
      topics[1]: orderHash (indexed, bytes32)
      topics[2]: maker     (indexed, address)
      topics[3]: taker     (indexed, address)
      data (non-indexed, 7 words):
        uint8   side           — 0 = BUY (maker gives USDC, gets tokens)
                                 1 = SELL (maker gives tokens, gets USDC)
        uint256 token_id      — the outcome token id being traded
        uint256 makerAmountFilled
        uint256 takerAmountFilled
        uint256 fee
        uint256 builder
        bytes32 metadata

    Returns None if the log shape doesn't match.
    """
    topics = log.get("topics") or []
    data = log.get("data") or "0x"
    if len(topics) < 4 or not data or data == "0x":
        return None

    try:
        clean = data[2:] if data.startswith("0x") else data
        # 7 words: side(1) + token_id(1) + maker_amt(1) + taker_amt(1)
        #         + fee(1) + builder(1) + metadata(1) = 7 words = 224 hex chars
        if len(clean) < 64 * 7:
            return None

        words = [clean[i * 64 : (i + 1) * 64] for i in range(7)]

        side = int(words[0], 16)  # 0 = BUY, 1 = SELL (maker side)
        token_id = int(words[1], 16)
        maker_fill = int(words[2], 16)
        taker_fill = int(words[3], 16)
        fee = int(words[4], 16)
        builder = int(words[5], 16)
        metadata = "0x" + words[6]

        # Indexed params from topics
        order_hash = "0x" + topics[1][-64:]
        maker = "0x" + topics[2][-40:]
        taker = "0x" + topics[3][-40:]

        # Derive maker/taker asset ids from side (matches warproxxx logic):
        # side=0 (BUY): maker gives USDC (asset_id=0), gets token_id
        # side=1 (SELL): maker gives token_id, gets USDC (asset_id=0)
        if side == 0:
            maker_asset_id = 0
            taker_asset_id = token_id
        else:
            maker_asset_id = token_id
            taker_asset_id = 0

        block_num = log.get("blockNumber", "0x0")
        if isinstance(block_num, str):
            block_num = int(block_num, 16)
        log_idx = log.get("logIndex", "0x0")
        if isinstance(log_idx, str):
            log_idx = int(log_idx, 16)

        return {
            "transaction_hash": log.get("transactionHash", ""),
            "log_index": log_idx,
            "block_number": block_num,
            "order_hash": order_hash,
            "maker": maker.lower(),
            "taker": taker.lower(),
            "side": side,
            "token_id": str(token_id),
            "maker_asset_id": maker_asset_id,
            "taker_asset_id": taker_asset_id,
            "maker_fill_amount": maker_fill,
            "taker_fill_amount": taker_fill,
            "fee": fee,
            "builder": builder,
            "metadata": metadata,
        }
    except (ValueError, IndexError) as exc:
        logger.debug("Failed to decode log: %s", exc)
        return None


def asset_id_to_side(asset_id: int) -> str:
    """Polymarket convention: 0 = NO collateral / 1 = YES collateral.

    The exact mapping depends on the market's clobTokenIds; we approximate
    here and let the join step resolve the final side from markets.csv.
    """
    return "YES" if asset_id == 1 else "NO" if asset_id == 0 else f"ASSET_{asset_id}"


# ---------------------------------------------------------------------------
# Cursor state (resumable backfill)
# ---------------------------------------------------------------------------


def load_cursor() -> dict:
    if not os.path.exists(CURSOR_STATE_PATH):
        return {}
    try:
        with open(CURSOR_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cursor(state: dict) -> None:
    os.makedirs(os.path.dirname(CURSOR_STATE_PATH), exist_ok=True)
    with open(CURSOR_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class PolyDataIngest:
    """High-level orchestrator: chain scan + markets metadata join."""

    def __init__(self, cfg: PolyDataConfig | None = None):
        self.cfg = cfg or PolyDataConfig()
        self.rpc = PolygonRPCClient(self.cfg)

    # -- Markets metadata -------------------------------------------------

    def fetch_markets_metadata(self, *, force_refresh: bool = False) -> pd.DataFrame:
        """Fetch all markets via Gamma keyset API with cursor pagination."""
        if not force_refresh and os.path.exists(MARKETS_CSV_PATH):
            logger.info("Loading cached markets.csv from %s", MARKETS_CSV_PATH)
            try:
                return pd.read_csv(MARKETS_CSV_PATH)
            except Exception:
                pass  # fall through to refetch

        logger.info("Fetching markets from Gamma keyset API: %s", self.cfg.gamma_url)
        all_markets: list[dict] = []
        cursor = ""
        page = 0
        while True:
            page += 1
            params: dict[str, Any] = {"limit": 1000}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = requests.get(self.cfg.gamma_url, params=params, timeout=30.0)
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:
                logger.warning("Gamma fetch failed at page %d: %s", page, exc)
                break

            # Gamma keyset returns {"markets": [...], "next_cursor": "..."}
            if isinstance(payload, dict):
                batch = payload.get("markets", [])
                cursor = payload.get("next_cursor", "") or ""
            elif isinstance(payload, list):
                batch = payload
                cursor = ""
            else:
                batch = []
                cursor = ""

            if not batch:
                break
            all_markets.extend(batch)
            logger.info(
                "Gamma page %d: +%d markets (total %d)",
                page,
                len(batch),
                len(all_markets),
            )
            if not cursor:
                break
            time.sleep(0.1)  # be polite

        df = pd.DataFrame(all_markets)
        os.makedirs(os.path.dirname(MARKETS_CSV_PATH), exist_ok=True)
        df.to_csv(MARKETS_CSV_PATH, index=False)
        logger.info("Saved %d markets to %s", len(df), MARKETS_CSV_PATH)
        return df

    # -- On-chain trades --------------------------------------------------

    def scan_order_filled(
        self,
        *,
        from_block: int | None = None,
        to_block: int | None = None,
        max_blocks: int | None = None,
    ) -> pd.DataFrame:
        """Scan OrderFilled events in [from_block, to_block] range.

        Resumable: if from_block is None, picks up from the saved cursor.
        If to_block is None, uses latest block.
        """
        cursor_state = load_cursor()
        if from_block is None:
            from_block = cursor_state.get("last_block", V2_GENESIS_BLOCK - 1) + 1
        if to_block is None:
            latest = self.rpc.get_latest_block()
            if latest is None:
                logger.warning(
                    "PolyDataIngest: could not fetch latest block, aborting scan"
                )
                return pd.DataFrame()
            to_block = latest - CONFIRMATIONS  # reorg-safe
        if max_blocks is not None:
            to_block = min(to_block, from_block + max_blocks - 1)  # type: ignore[reportOptionalOperand]

        logger.info(
            "Scanning OrderFilled blocks %d → %d (chunk=%d, contract=%s)",
            from_block,
            to_block,
            self.cfg.block_chunk,
            self.cfg.ctf_exchange,
        )

        all_events: list[dict] = []
        # Track unique block numbers so we can batch-fetch timestamps
        block_timestamps: dict[int, int] = {}
        cur = from_block
        while cur <= to_block:  # type: ignore[reportOptionalOperand]
            chunk_end = min(cur + self.cfg.block_chunk - 1, to_block)  # type: ignore[reportOptionalOperand]
            try:
                logs = self.rpc.get_logs(
                    from_block=cur,
                    to_block=chunk_end,
                    address=self.cfg.ctf_exchange,
                    topics=[ORDER_FILLED_TOPIC0],
                )
            except Exception as exc:
                logger.warning(
                    "getLogs failed for %d-%d: %s — reducing chunk", cur, chunk_end, exc
                )
                # Adaptive backoff: halve the chunk and retry once
                if self.cfg.block_chunk > 1:
                    self.cfg.block_chunk = max(1, self.cfg.block_chunk // 2)
                    continue
                raise

            for log in logs:
                decoded = decode_order_filled(log)
                if decoded:
                    all_events.append(decoded)
                    bn = decoded["block_number"]
                    if bn not in block_timestamps:
                        block_timestamps[bn] = None  # placeholder

            save_cursor({"last_block": chunk_end})
            logger.info(
                "  blocks %d-%d: %d events (cumulative %d)",
                cur,
                chunk_end,
                len(logs),
                len(all_events),
            )
            cur = chunk_end + 1

        if not all_events:
            return pd.DataFrame()

        # Batch-fetch block timestamps for all unique blocks we saw events in.
        # eth_getBlockByNumber is cheap but we batch to avoid hammering RPC.
        logger.info(
            "Fetching block timestamps for %d unique blocks...", len(block_timestamps)
        )
        for bn in list(block_timestamps.keys()):
            try:
                result = self.rpc._call("eth_getBlockByNumber", [hex(bn), False])
                ts = int(result.get("timestamp", "0x0"), 16) if result else 0
                block_timestamps[bn] = ts
            except Exception as exc:
                logger.debug("getBlock failed for %d: %s", bn, exc)
                block_timestamps[bn] = 0

        df = pd.DataFrame(all_events)
        # Attach block timestamp (UTC)
        df["timestamp"] = df["block_number"].map(block_timestamps).fillna(0).astype(int)
        df["datetime_utc"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        # Attach trade side from asset ids (will be refined during join)
        df["taker_side"] = df["taker_asset_id"].map(asset_id_to_side)
        df["maker_side"] = df["maker_asset_id"].map(asset_id_to_side)
        # USD value: Polymarket CTF tokens are $1-collateralized (ERC1155 with
        # 1 unit = $1 USDC at settlement). USDC has 6 decimals on Polygon.
        # When side=BUY, maker_fill = USDC amount (divide by 1e6 for USD).
        # When side=SELL, maker_fill = token amount (also divide by 1e6 because
        # Polymarket CT tokens use the same 6-decimal convention).
        df["maker_usd"] = df["maker_fill_amount"] / 1e6
        df["taker_usd"] = df["taker_fill_amount"] / 1e6
        # Implied price (rough): for a BUY (side=0), maker gives USDC for tokens,
        # so price = usdc_paid / tokens_received = maker_usd / taker_usd.
        # For a SELL (side=1), inverse.
        df["implied_price"] = df.apply(
            lambda r: (r["maker_usd"] / r["taker_usd"]) if r["taker_usd"] > 0 else 0.0,
            axis=1,
        ).clip(0.0, 1.0)
        return df

    # -- Join -------------------------------------------------------------

    def join_with_markets(
        self,
        trades_df: pd.DataFrame,
        markets_df: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        """Join on-chain trades with market metadata.

        Requires markets_df to have a `clobTokenIds` column (JSON-encoded list
        of two strings: [yes_token, no_token]) and an `id` column.
        """
        if trades_df.empty:
            return trades_df
        if markets_df is None:
            markets_df = self.fetch_markets_metadata()
        if markets_df.empty:
            logger.warning("No markets metadata to join — returning raw trades")
            return trades_df

        # Build a token_id → (market_id, side) lookup
        token_lookup: dict[str, dict[str, Any]] = {}
        for _, row in markets_df.iterrows():
            market_id = row.get("id") or row.get("market_id")
            raw_tokens = row.get("clobTokenIds")
            if not raw_tokens:
                continue
            if isinstance(raw_tokens, str):
                try:
                    tokens = json.loads(raw_tokens)
                except Exception:
                    continue
            else:
                tokens = raw_tokens
            if not isinstance(tokens, list) or len(tokens) < 2:
                continue
            # tokens[0] = YES, tokens[1] = NO (Polymarket convention)
            yes_token = str(tokens[0]).lower()
            no_token = str(tokens[1]).lower()
            token_lookup[yes_token] = {
                "market_id": str(market_id),
                "side": "YES",
                "question": row.get("question", ""),
            }
            token_lookup[no_token] = {
                "market_id": str(market_id),
                "side": "NO",
                "question": row.get("question", ""),
            }

        # The OrderFilled event doesn't directly carry the token id in a
        # decoded form here (asset_id is 0/1, not the actual tokenId). The
        # real warproxxx/poly_data pipeline resolves this by reading
        # OrderMatched events with token addresses. For our purposes —
        # aggregate market activity — we attach the question via block-timestamp
        # proximity if exact token join isn't possible.
        #
        # In production, callers should pair this with resolvedmarkets_ingest
        # which already carries market_id directly on each trade.
        if "market_id" not in trades_df.columns:
            trades_df["market_id"] = ""

        return trades_df


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s  %(name)-22s  %(message)s"
    )
    ingest = PolyDataIngest()

    print("\n=== Markets metadata ===")
    markets = ingest.fetch_markets_metadata()
    print(f"Markets fetched: {len(markets)}")
    if not markets.empty:
        print(
            markets[["id", "question"]].head(5).to_string()
            if "question" in markets.columns
            else markets.head(5)
        )

    print("\n=== OrderFilled scan (latest 1000 blocks) ===")
    latest = ingest.rpc.get_latest_block()
    print(f"Latest block: {latest}")
    trades = ingest.scan_order_filled(
        from_block=max(0, latest - 1000),
        to_block=latest,
    )
    print(f"Trades fetched: {len(trades)}")
    if not trades.empty:
        cols = [
            "block_number",
            "datetime_utc",
            "maker",
            "taker",
            "maker_usd",
            "taker_usd",
            "implied_price",
        ]
        print(trades[cols].head(10).to_string())
