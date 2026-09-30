"""Dropping the trips a merge input repeats from the inputs before it."""

from __future__ import annotations

import numpy as np
import pandas as pd

from transitio.gtfs._patch import _drop_trip_rows
from transitio.gtfs._schedule import service_dates, trip_signatures


def drop_duplicate_trips(merged, table_sets, positions):
    """Drop from ``merged`` the trips that repeat an earlier input's (see
    :func:`~transitio.gtfs.merge_tables`) and link their stops.

    ``table_sets`` are the inputs' prefixed tables and ``positions`` their
    positions in the report; returns the report's counts.
    """
    dropped, matched, unexpanded = _find_duplicates(table_sets)
    gone = set().union(*dropped)
    links = _stop_links(merged, matched, gone)
    if len(links):
        rows = pd.DataFrame(
            {
                "from_stop_id": np.r_[links["later"], links["earlier"]],
                "to_stop_id": np.r_[links["earlier"], links["later"]],
                "transfer_type": "2",
                "min_transfer_time": "0",
            }
        )
        rows = pd.concat([merged.get("transfers.txt"), rows], ignore_index=True)
        merged["transfers.txt"] = rows.fillna("")
    _drop_trip_rows(merged, gone, [])
    return {
        "dropped": len(gone),
        "by_feed": {at: len(ids) for at, ids in zip(positions, dropped) if ids},
        "unexpanded_services": unexpanded,
        "stop_links": len(links),
    }


def _named_trips(tables, name, *columns):
    table = tables.get(name, pd.DataFrame())
    values = [table[column] for column in columns if column in table.columns]
    return set().union(*(set(value[value.str.strip() != ""]) for value in values))


def _find_duplicates(table_sets):
    """``(dropped, matched, unexpanded)``: each input's dropped trip ids,
    the ``(dropped trip, earlier trip)`` pairs matched, and the number of
    services not expanded."""
    signed, dates = [], []
    unexpanded = 0
    for position, tables in enumerate(table_sets):
        trips = trip_signatures(tables)
        service_days, left_out = service_dates(tables)
        unexpanded += len(left_out)
        never = _named_trips(tables, "transfers.txt", "from_trip_id", "to_trip_id")
        compared = ~trips["service_id"].isin(left_out) & ~trips["trip_id"].isin(never)
        signed.append(trips[compared].assign(input=position))
        dates.append(service_days)
    signed = pd.concat(signed, ignore_index=True)
    # A signature only one input carries drops nothing.
    shared = signed.groupby("signature")["input"].transform("nunique") > 1
    signed = signed[shared].sort_values(["input", "trip_id"], ignore_index=True)
    dates = pd.concat(dates, ignore_index=True)
    services, names = pd.factorize(dates["service_id"])
    allocation = _Allocation(
        signed["signature"].to_numpy(),
        names.get_indexer(signed["service_id"]),
        services,
        dates["date"].to_numpy().astype("datetime64[D]").astype("int64"),
    )
    codes = pd.Series(signed.index, index=signed["trip_id"])
    dropped = []
    for position, tables in enumerate(table_sets):
        blocks = _blocks(tables.get("trips.txt"), codes)
        # Capacity counts within one input, so a trip every earlier input
        # repeats is dropped from each later one.
        allocation.used.clear()
        gone = set()
        for trip_ids, members in blocks if position else ():
            if allocation.cover(members):
                gone.update(trip_ids)
        allocation.retain([members for ids, members in blocks if ids[0] not in gone])
        dropped.append(gone)
    trip_ids = signed["trip_id"].to_numpy()
    matched = {(trip_ids[a], trip_ids[b]) for a, b in allocation.matched}
    return dropped, matched, unexpanded


def _blocks(trips, codes):
    """One input's blocks as ``(trip ids, codes)`` (-1: never compared), in
    the order they are considered: trips without a block_id by trip id,
    then blocks by block_id."""
    if trips is None or "trip_id" not in trips.columns:
        return []
    trips = trips.drop_duplicates("trip_id").sort_values("trip_id")
    block = trips.get("block_id", pd.Series("", index=trips.index))
    frame = pd.DataFrame(
        {
            "trip_id": trips["trip_id"].to_numpy(),
            "code": codes.reindex(trips["trip_id"]).fillna(-1).astype(int).to_numpy(),
            "block": block.str.strip().to_numpy(),
        }
    )
    alone = frame[frame["block"] == ""]
    blocks = [([trip], [code]) for trip, code in zip(alone["trip_id"], alone["code"])]
    for _, members in frame[frame["block"] != ""].groupby("block", sort=True):
        blocks.append((list(members["trip_id"]), list(members["code"])))
    return blocks


class _Allocation:
    """The retained earlier trips and their service days' capacity: each
    covers at most one later trip per day it runs."""

    def __init__(self, signatures, trip_services, services, days):
        order = np.lexsort((days, services))
        # The days of service s are days[bounds[s]:bounds[s + 1]].
        self.days = days[order]
        self.bounds = np.searchsorted(
            services[order], np.arange(services.max(initial=-1) + 2)
        )
        self.trip_services = trip_services  # -1: a service without days
        self.used = {}  # trip code -> which of its days are taken
        self.signatures = signatures
        self.retained = {}  # signature -> retained codes, by input, trip id
        self.blocks = []  # retained blocks' codes, by input, block_id
        self.blocks_with = {}  # signature -> (first code, index into blocks)
        self.matched = set()  # (later code, earlier code)

    def _runs(self, code):
        service = self.trip_services[code]
        if service < 0:
            return self.days[:0]
        return self.days[self.bounds[service] : self.bounds[service + 1]]

    def _free(self, wanted, code):
        """Where each day of ``wanted`` falls among ``code``'s days, -1 where
        it does not run then or that day's capacity is taken."""
        offered = self._runs(code)
        if not len(offered):
            return np.full(len(wanted), -1)
        at = np.minimum(np.searchsorted(offered, wanted), len(offered) - 1)
        taken = self.used.get(code)
        free = offered[at] == wanted
        if taken is not None:
            free &= ~taken[at]
        return np.where(free, at, -1)

    def _take(self, code, at):
        taken = self.used.setdefault(code, np.zeros(len(self._runs(code)), dtype=bool))
        taken[at] = True

    def cover(self, codes):
        """Whether retained earlier trips cover a later block, taking their
        capacity when they do."""
        for code in codes:
            if code < 0 or not len(self._runs(code)):
                return False
            if self.signatures[code] not in self.retained:
                return False
        pairs = self._trip(codes[0]) if len(codes) == 1 else self._block(codes)
        self.matched.update(pairs or ())
        return pairs is not None

    def _trip(self, code):
        wanted = self._runs(code)
        owners = units = np.full(len(wanted), -1)
        for earlier in self.retained[self.signatures[code]]:
            free = self._free(wanted, earlier)
            take = (owners < 0) & (free >= 0)
            owners, units = np.where(take, earlier, owners), np.where(take, free, units)
        if (owners < 0).any():
            return None
        owned = np.unique(owners)
        for earlier in owned:
            self._take(earlier, units[owners == earlier])
        return {(code, int(earlier)) for earlier in owned}

    def _block(self, codes):
        for _, index in self.blocks_with.get(self.signatures[codes[0]], ()):
            # Per later trip, the block's trips that match it and have
            # capacity on all its days.
            options = []
            for code in codes:
                fits = {}
                for earlier in self.blocks[index]:
                    if self.signatures[earlier] == self.signatures[code]:
                        free = self._free(self._runs(code), earlier)
                        if (free >= 0).all():
                            fits[earlier] = free
                options.append(fits)
            chosen = _one_to_one(options)
            if chosen is not None:
                for earlier, fits in zip(chosen, options):
                    self._take(earlier, fits[earlier])
                return set(zip(codes, chosen))
        return None

    def retain(self, blocks):
        """Make one input's kept blocks earlier trips for the inputs after."""
        for code in sorted(code for members in blocks for code in members):
            if code >= 0:
                self.retained.setdefault(self.signatures[code], []).append(code)
        first = {}  # signature -> the block's first trip with it
        for members in blocks:
            members = sorted(code for code in members if code >= 0)
            # Only an earlier block of several trips completes a later one.
            if len(members) > 1:
                self.blocks.append(members)
                for code in reversed(members):
                    first[self.signatures[code]] = code
                for signature, code in first.items():
                    found = self.blocks_with.setdefault(signature, [])
                    found.append((code, len(self.blocks) - 1))
                first.clear()
        # Candidate blocks go by input, then by their first matching trip.
        for found in self.blocks_with.values():
            found.sort()


def _one_to_one(options):
    """A distinct option for every entry, earlier options preferred, found
    along augmenting paths; None when there is none."""
    owners = {}
    for root in range(len(options)):
        # Depth-first with an explicit stack: each frame but the top has
        # taken the option owned by the frame above it.
        seen, stack, taken = set(), [(root, iter(options[root]))], []
        while stack:
            option = next((o for o in stack[-1][1] if o not in seen), None)
            if option is None:
                stack.pop()
                if taken:
                    taken.pop()
                continue
            seen.add(option)
            taken.append(option)
            if option in owners:
                stack.append((owners[option], iter(options[owners[option]])))
                continue
            for (entry, _), won in zip(stack, taken):
                owners[won] = entry
            break
        else:
            return None
    chosen = {entry: option for option, entry in owners.items()}
    return [chosen[entry] for entry in range(len(options))]


def _stop_links(merged, matched, gone):
    """The ``(later, earlier)`` stop pairs to link: each dropped trip's
    stops and its matched earlier trip's, position by position, where a
    kept trip still serves the later stop and no transfer joins the two."""
    if not matched:
        return pd.DataFrame(columns=["later", "earlier"])
    stop_times = merged["stop_times.txt"]
    involved = stop_times[stop_times["trip_id"].isin({t for p in matched for t in p})]
    sequence = pd.to_numeric(involved["stop_sequence"].str.strip(), errors="coerce")
    ordered = involved[["trip_id", "stop_id"]].assign(sequence=sequence)
    ordered = ordered.sort_values(["trip_id", "sequence"])
    ordered["position"] = ordered.groupby("trip_id").cumcount()
    pairs = pd.DataFrame(list(matched), columns=["later", "earlier_trip"])
    both = pairs.merge(ordered, left_on="later", right_on="trip_id").merge(
        ordered, left_on=["earlier_trip", "position"], right_on=["trip_id", "position"]
    )
    links = pd.DataFrame(
        {"later": both["stop_id_x"].to_numpy(), "earlier": both["stop_id_y"].to_numpy()}
    ).drop_duplicates()
    served = stop_times.loc[~stop_times["trip_id"].isin(gone), "stop_id"]
    links = links[links["later"].isin(served) & (links["later"] != links["earlier"])]
    transfers = merged.get("transfers.txt", pd.DataFrame())
    if {"from_stop_id", "to_stop_id"} <= set(transfers.columns):
        joined = pd.MultiIndex.from_frame(transfers[["from_stop_id", "to_stop_id"]])
        for ends in (["later", "earlier"], ["earlier", "later"]):
            links = links[~pd.MultiIndex.from_frame(links[ends]).isin(joined)]
    return links.reset_index(drop=True)
