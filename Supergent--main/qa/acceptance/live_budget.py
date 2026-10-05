"""Opt-in, cross-process OpenRouter QA budget. No credentials in the ledger.

Every actual completion reserves a worst-case token cost before dispatch.
Provider-reported per-generation cost settles it; unknown charges retain the
reservation. Other users' traffic never replenishes or consumes this ledger.
"""
from __future__ import annotations
import hashlib
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path


class BudgetError(RuntimeError):
    pass


def number(value):
    if isinstance(value, bool): raise BudgetError("Invalid budget number")
    try: result = Decimal(str(value))
    except Exception as exc: raise BudgetError("Invalid budget number") from exc
    if not result.is_finite() or result < 0: raise BudgetError("Invalid budget number")
    return result


class LiveBudget:
    def __init__(self, path, *, api_key=None):
        self.path = Path(path).resolve()
        self.api_key = api_key

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(".lock")
        with lock_path.open("a+b") as handle:
            if handle.tell() == 0: handle.write(b"0"); handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else None
                box = [data]
                yield box
                if box[0] is not None:
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                                         suffix=".tmp", delete=False) as stream:
                            temporary = Path(stream.name)
                            json.dump(box[0], stream, indent=2)
                            stream.flush(); os.fsync(stream.fileno())
                        os.replace(temporary, self.path)
                    finally:
                        if temporary is not None: temporary.unlink(missing_ok=True)
            finally:
                handle.seek(0)
                if os.name == "nt": msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def initialize(self, *, additional_usd, model, pricing):
        cap = number(additional_usd)
        # CLI creation is an explicit opt-in. It cannot silently raise the
        # previously approved cap or reset a used ledger on another invocation.
        if cap <= 0 or cap > Decimal("1"): raise BudgetError("This approved QA allowance is at most one additional dollar")
        if not {"prompt", "completion"}.issubset(pricing):
            raise BudgetError("Official prompt and completion prices are required")
        prices = {key: str(number(pricing.get(key, "0"))) for key in ("prompt", "completion", "request")}
        with self.transaction() as box:
            if box[0] is None:
                box[0] = {"schema":"wise.live-budget.v1", "approved_additional_usd":str(cap),
                    "models":{model:prices}, "reservations":{}, "generations":{}, "turns":{}, "active_turn":None,
                    "created_at":time.time(), "scope":"ONLY generation costs attributed to this isolated QA ledger"}
            elif number(box[0]["approved_additional_usd"]) != cap:
                raise BudgetError("Existing approved allowance cannot be changed or reset")
            elif model not in box[0]["models"]:
                box[0]["models"][model] = prices
            else:
                # A subsequent run must never use an older, lower tariff to
                # admit calls after the official provider prices increase.
                previous = box[0]["models"][model]
                box[0]["models"][model] = {key: str(max(number(previous[key]), number(prices[key])))
                                           for key in prices}
        return self.summary()

    @staticmethod
    def committed(data):
        settled = sum((number(row["cost_usd"]) for row in data["generations"].values()), Decimal("0"))
        pending = sum((number(row["reserved_usd"]) for row in data["reservations"].values()
                       if row["state"] == "PENDING"), Decimal("0"))
        return settled, pending

    def begin_turn(self, *, max_requests=24):
        if not isinstance(max_requests, int) or not 1 <= max_requests <= 32: raise BudgetError("Invalid turn request bound")
        with self.transaction() as box:
            data = box[0]
            if data is None: raise BudgetError("No approved budget ledger")
            if data["active_turn"] is not None: raise BudgetError("Another paid QA turn remains active; reconcile it before retrying")
            identifier = uuid.uuid4().hex
            data["turns"][identifier] = {"max_requests":max_requests,"requests":0,"state":"OPEN","started_at":time.time()}
            data["active_turn"] = identifier
        return identifier

    def end_turn(self, identifier):
        with self.transaction() as box:
            data = box[0]
            if data["active_turn"] != identifier: raise BudgetError("Turn identity does not match")
            data["turns"][identifier]["state"] = "CLOSED"
            data["active_turn"] = None

    def reserve(self, model, *, input_byte_bound, max_tokens):
        if not isinstance(max_tokens, int) or not 1 <= max_tokens <= 4096: raise BudgetError("QA completion output exceeds its bounded limit")
        if not isinstance(input_byte_bound, int) or not 0 <= input_byte_bound <= 180_000: raise BudgetError("QA input exceeds its bounded limit")
        with self.transaction() as box:
            data = box[0]
            if data is None or data["active_turn"] is None: raise BudgetError("No explicitly active paid QA turn")
            turn = data["turns"][data["active_turn"]]
            if turn["requests"] >= turn["max_requests"]: raise BudgetError("Paid QA turn exhausted its request count")
            prices = data["models"].get(model)
            if prices is None: raise BudgetError("Model was not admitted with official prices")
            bound = ((input_byte_bound+1024)*number(prices["prompt"]) + max_tokens*number(prices["completion"]) + number(prices["request"])) * Decimal("1.10")
            settled, pending = self.committed(data)
            if settled+pending+bound > number(data["approved_additional_usd"]): raise BudgetError("Additional paid QA allowance is insufficient; no request dispatched")
            identifier=uuid.uuid4().hex
            data["reservations"][identifier]={"model":model,"reserved_usd":str(bound),"state":"PENDING",
                "turn_id":data["active_turn"],"created_at":time.time()}
            turn["requests"]+=1
        return identifier

    def settle(self, identifier, response):
        generation = str(response.get("id") or "")
        cost = response.get("usage", {}).get("cost")
        # Inline usage is the normal path. Metadata GET is read-only and is
        # not another completion. Missing/late accounting retains the reserve.
        if cost is None and generation and self.api_key:
            import httpx
            for attempt in range(3):
                try:
                    result = httpx.get("https://openrouter.ai/api/v1/generation", params={"id":generation},
                        headers={"Authorization":"Bearer "+self.api_key},timeout=10,trust_env=False)
                    result.raise_for_status(); cost=result.json()["data"]["total_cost"];break
                except Exception:
                    if attempt < 2: time.sleep(.5)
        if not generation or cost is None: return False
        amount=number(cost)
        with self.transaction() as box:
            data=box[0]; row=data["reservations"][identifier]
            if row["state"]=="SETTLED": return True
            if generation in data["generations"]: raise BudgetError("Generation accounting identity was reused")
            data["generations"][generation]={"cost_usd":str(amount),"model":row["model"],"reservation":identifier}
            row.update(state="SETTLED",generation_id=generation)
        return True

    def summary(self):
        with self.transaction() as box:
            data=box[0]
            if data is None: raise BudgetError("No approved ledger")
            settled,pending=self.committed(data)
            return {"approved_additional_usd":float(number(data["approved_additional_usd"])),
                "attributed_generation_cost_usd":float(settled), "pending_worst_case_usd":float(pending),
                "remaining_admission_usd":float(number(data["approved_additional_usd"])-settled-pending),
                "actual_generations":len(data["generations"]),"dispatched_requests":len(data["reservations"]),
                "active_turn":data["active_turn"],"ledger":str(self.path)}

    def summarize_turns(self, identifiers):
        """Attribute only explicitly owned turns, not a shared ledger window.

        Another runner may dispatch during an unpaid soak. A before/after
        total remains useful for the shared cap, but is not this run's cost.
        This method neither releases reserves nor changes the approved cap.
        """
        if not isinstance(identifiers, (list, tuple)) or len(identifiers) > 256:
            raise BudgetError("Bounded owned turn identities are required")
        if any(not isinstance(value, str) or not value for value in identifiers):
            raise BudgetError("Invalid owned turn identity")
        owned = set(identifiers)
        if len(owned) != len(identifiers):
            raise BudgetError("Duplicate owned turn identity")
        with self.transaction() as box:
            data = box[0]
            if data is None or not owned.issubset(data["turns"]):
                raise BudgetError("Unknown owned turn identity")
            reservations = {key: row for key, row in data["reservations"].items()
                            if row.get("turn_id") in owned}
            generations = [row for row in data["generations"].values()
                           if row.get("reservation") in reservations]
            settled = sum((number(row["cost_usd"]) for row in generations), Decimal("0"))
            pending = sum((number(row["reserved_usd"]) for row in reservations.values()
                           if row["state"] == "PENDING"), Decimal("0"))
            return {"scope": "EXACT_OWNED_TURNS_NOT_SHARED_WINDOW_DELTA",
                    "owned_turn_ids": list(identifiers),
                    "attributed_generation_cost_usd": float(settled),
                    "pending_worst_case_usd": float(pending),
                    "actual_generations": len(generations),
                    "dispatched_requests": len(reservations)}


def provider_gate(api_key, base_url):
    """Only an explicitly opted-in isolated QA child process gets this gate."""
    path=os.environ.get("WISE_QA_BUDGET_PATH")
    if not path: return None
    from urllib.parse import urlsplit
    if urlsplit(base_url).hostname != "openrouter.ai": raise BudgetError("The paid QA gate admits only the approved OpenRouter provider")
    return LiveBudget(path,api_key=api_key)
