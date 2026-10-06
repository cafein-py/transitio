"""Dropping the trips a merge input repeats from the inputs before it."""

from __future__ import annotations

import numpy as np
import pandas as pd

from transitio.gtfs._near import near_matches
from transitio.gtfs._patch import _drop_trip_rows
from transitio.gtfs._schedule import service_dates, trip_signatures


def drop_duplicate_trips(merged, table_sets, positions, near=True):
    """Drop from ``merged`` the trips that repeat an earlier input's (see
    :func:`~transitio.gtfs.merge_tables`), with ``near`` also those that
    nearly repeat one (see :func:`~transitio.gtfs._near.near_matches`), and
    link their stops.

    ``table_sets`` are the inputs' prefixed tables and ``positions`` their
    positions in the report; returns the report's counts.
    """
    dropped, matched, aligned, unexpanded = _find_duplicates(table_sets, near)
    gone = set().union(*dropped)
    links = _stop_links(merged, matched, aligned, gone)
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
    # A stop time is unaligned when none of its trip's pairs aligns it.
    stops = aligned.groupby(["later", "position"])["earlier_stop"].count()
    return {
        "dropped": len(gone),
        "by_feed": {at: len(ids) for at, ids in zip(positions, dropped) if ids},
        "unexpanded_services": unexpanded,
        "stop_links": len(links),
        "near_matches": aligned["later"].nunique(),
        "unaligned_stops": int((stops == 0).sum()),
    }


def _named_trips(tables, name, *columns):
    table = tables.get(name, pd.DataFrame())
    values = [table[column] for column in columns if column in table.columns]
    return set().union(*(set(value[value.str.strip() != ""]) for value in values))


def repeated_trips(table_sets, near=True, day=None):
    """Per input, ``(trip ids, earlier, in scope)``: the ids of the trips
    that repeat, or with ``near`` nearly repeat, trips kept from the inputs
    before it, as :func:`~transitio.gtfs.merge_tables` leaves them out; the
    positions of the inputs whose trips they repeat, sorted; and the ids of
    its trips in scope, which a trip that cannot be compared stays among.

    ``table_sets`` are the inputs' unprefixed tables; an empty one is not
    compared. With a ``day``, a date, the trips in scope are those running
    that day and those whose services cannot be read or are not declared,
    and each earlier trip covers one later trip on it; without one, every
    trip is in scope.
    """
    from transitio.gtfs._merge import _prefix_feed

    within = None if day is None else (day, day)
    present = [position for position, tables in enumerate(table_sets) if tables]
    prefixed, scope = [], [set() for _ in table_sets]
    for position in present:
        tables = table_sets[position]
        if within is not None:
            tables = _running(tables, within)
        trips = tables.get("trips.txt")
        if trips is not None and "trip_id" in trips.columns:
            scope[position] = set(trips["trip_id"])
        prefixed.append(_prefix_feed(tables, f"f{position}", set()))
    if len(prefixed) < 2:
        return [(set(), [], ids) for ids in scope]
    dropped, exact, aligned, _ = _find_duplicates(prefixed, near, within)
    pairs = set(exact) | set(zip(aligned["later"], aligned["earlier"]))
    found = [(set(), set()) for _ in table_sets]
    for position, gone in zip(present, dropped):
        found[position][0].update(trip.split(":", 1)[1] for trip in gone)
    for later, earlier in pairs:
        found[int(later.split(":", 1)[0][1:])][1].add(int(earlier.split(":", 1)[0][1:]))
    return [(gone, sorted(of), ids) for (gone, of), ids in zip(found, scope)]


def _running(tables, within):
    """``tables`` with only the trips running ``within``, a ``(first, last)``
    pair of dates, or whose services cannot be read or neither calendar file
    declares, and their stop times and frequencies."""
    trips = tables.get("trips.txt")
    if trips is None or "service_id" not in trips.columns:
        return tables
    dates, unexpanded = service_dates(tables, within)
    declared = _named_trips(tables, "calendar.txt", "service_id")
    declared |= _named_trips(tables, "calendar_dates.txt", "service_id")
    service = trips["service_id"]
    readable = service.isin(declared) & ~service.isin(unexpanded)
    trips = trips[service.isin(dates["service_id"]) | ~readable]
    out = {**tables, "trips.txt": trips}
    for name in ("stop_times.txt", "frequencies.txt"):
        table = tables.get(name)
        if table is not None and "trip_id" in table.columns:
            out[name] = table[table["trip_id"].isin(trips["trip_id"])]
    return out


def _find_duplicates(table_sets, near, within=None):
    """``(dropped, matched, aligned, unexpanded)``: each input's dropped
    trip ids, the ``(dropped trip, earlier trip)`` pairs matched exactly,
    with ``near`` the stops of the pairs matched near (see
    :func:`~transitio.gtfs._near.near_matches`), and the number of services
    not expanded. ``within``, a ``(first, last)`` pair of dates, counts
    only the days between them."""
    signed, dates, stops = [], [], []
    unexpanded = 0
    for position, tables in enumerate(table_sets):
        trips = trip_signatures(tables, with_stops=near)
        if near:
            trips, rows = trips
        service_days, left_out = service_dates(tables, within)
        unexpanded += len(left_out)
        never = _named_trips(tables, "transfers.txt", "from_trip_id", "to_trip_id")
        compared = ~trips["service_id"].isin(left_out) & ~trips["trip_id"].isin(never)
        signed.append(trips[compared].assign(input=position))
        dates.append(service_days)
        if near:
            stops.append(rows[rows["trip_id"].isin(signed[-1]["trip_id"])])
    signed = pd.concat(signed, ignore_index=True)
    if not near:
        # A signature only one input carries drops nothing.
        shared = signed.groupby("signature")["input"].transform("nunique") > 1
        signed = signed[shared]
    signed = signed.sort_values(["input", "trip_id"], ignore_index=True)
    dates = pd.concat(dates, ignore_index=True)
    services, names = pd.factorize(dates["service_id"])
    days = dates["date"].to_numpy().astype("datetime64[D]").astype("int64")
    allocation = _Allocation(
        signed["signature"].to_numpy(),
        names.get_indexer(signed["service_id"]),
        services,
        days,
    )
    codes = pd.Series(signed.index, index=signed["trip_id"])
    runs = pd.DataFrame({"service": services, "day": days})

    def share_a_day(pairs):
        found = pd.DataFrame(
            {
                end: allocation.trip_services[codes.loc[pairs[end]].to_numpy()]
                for end in ("later", "earlier")
            }
        )
        both = found.drop_duplicates().merge(runs, left_on="later", right_on="service")
        both = both.merge(runs, left_on=["earlier", "day"], right_on=["service", "day"])
        common = pd.MultiIndex.from_frame(both[["later", "earlier"]])
        return pd.MultiIndex.from_frame(found).isin(common)

    dropped, retained, aligned = [], [], []
    for position, tables in enumerate(table_sets):
        blocks = _blocks(tables.get("trips.txt"), codes)
        # Capacity counts within one input, so a trip every earlier input
        # repeats is dropped from each later one.
        allocation.used.clear()
        gone = set()
        if position:
            unpaired = allocation.pair(np.flatnonzero(signed["input"] == position))
            if near and unpaired:
                later = stops[position]
                later = later[later["trip_id"].isin(signed["trip_id"].iloc[unpaired])]
                pairs, paired = near_matches(pd.concat(retained), later, share_a_day)
                allocation.pair_near(
                    *(codes.loc[pairs[end]].to_numpy() for end in ("later", "earlier"))
                )
                aligned.append(paired)
        for trip_ids, members in blocks if position else ():
            if allocation.cover(members):
                gone.update(trip_ids)
        allocation.retain([members for ids, members in blocks if ids[0] not in gone])
        if near:
            retained.append(stops[position][~stops[position]["trip_id"].isin(gone)])
        dropped.append(gone)
    trip_ids = signed["trip_id"].to_numpy()
    matched = {(trip_ids[a], trip_ids[b]) for a, b in allocation.matched}
    near_trips = set(trip_ids[sorted(allocation.near)])
    exact = {pair for pair in matched if pair[0] not in near_trips}
    columns = ["later", "earlier", "position", "later_stop", "earlier_stop"]
    aligned = pd.concat([pd.DataFrame(columns=columns), *aligned], ignore_index=True)
    ends = pd.MultiIndex.from_frame(aligned[["later", "earlier"]])
    return dropped, exact, aligned[ends.isin(matched - exact)], unexpanded


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
        # later code -> the retained codes it may repeat, by input, trip id:
        # those of its signature, else those it nearly repeats
        self.partners = {}
        self.near = set()  # later codes partnered by near repeats
        self.blocks = []  # retained blocks' codes, by input, block_id
        self.block_of = {}  # code -> index into blocks
        self.matched = set()  # (later code, earlier code)

    def pair(self, codes):
        """Partner each later trip of ``codes`` with the retained trips of
        its signature; returns those without any."""
        unpaired = []
        for code in codes:
            partners = self.retained.get(self.signatures[code])
            if partners:
                self.partners[code] = list(partners)
            else:
                unpaired.append(code)
        return unpaired

    def pair_near(self, later, earlier):
        """Partner each ``later`` trip with the ``earlier`` one beside it."""
        for code, partner in sorted(zip(later.tolist(), earlier.tolist())):
            self.partners.setdefault(code, []).append(partner)
            self.near.add(code)

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
            if code < 0 or not len(self._runs(code)) or code not in self.partners:
                return False
        pairs = self._trip(codes[0]) if len(codes) == 1 else self._block(codes)
        self.matched.update(pairs or ())
        return pairs is not None

    def _trip(self, code):
        wanted = self._runs(code)
        owners = units = np.full(len(wanted), -1)
        for earlier in self.partners[code]:
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
        partners = [set(self.partners[code]) for code in codes]
        # Candidate blocks go by input, then by their first partner.
        candidates = (self.block_of.get(code) for code in self.partners[codes[0]])
        for index in dict.fromkeys(index for index in candidates if index is not None):
            # Per later trip, the block's partners that have capacity on
            # all its days.
            options = []
            for code, allowed in zip(codes, partners):
                fits = {}
                for earlier in self.blocks[index]:
                    if earlier in allowed:
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
        for members in blocks:
            members = sorted(code for code in members if code >= 0)
            # Only an earlier block of several trips completes a later one.
            if len(members) > 1:
                self.block_of.update(dict.fromkeys(members, len(self.blocks)))
                self.blocks.append(members)


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


def _stop_links(merged, matched, aligned, gone):
    """The ``(later, earlier)`` stop pairs to link: each dropped trip's
    stops and its matched earlier trip's, position by position, or as
    ``aligned`` when matched near, where a kept trip still serves the later
    stop and no transfer joins the two."""
    near = aligned.loc[aligned["earlier_stop"].notna(), ["later_stop", "earlier_stop"]]
    if not matched and near.empty:
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
    )
    near = near.set_axis(["later", "earlier"], axis=1)
    links = pd.concat([links, near], ignore_index=True).drop_duplicates()
    served = stop_times.loc[~stop_times["trip_id"].isin(gone), "stop_id"]
    links = links[links["later"].isin(served) & (links["later"] != links["earlier"])]
    transfers = merged.get("transfers.txt", pd.DataFrame())
    if {"from_stop_id", "to_stop_id"} <= set(transfers.columns):
        joined = pd.MultiIndex.from_frame(transfers[["from_stop_id", "to_stop_id"]])
        for ends in (["later", "earlier"], ["earlier", "later"]):
            links = links[~pd.MultiIndex.from_frame(links[ends]).isin(joined)]
    return links.reset_index(drop=True)
