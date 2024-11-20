"""
Phil Budne @ Media Cloud
Started October 23, 2024

Take Elasticsearch "profile" response section and output for
consumption by flamegraph.pl

See: https://github.com/brendangregg/FlameGraph
and https://www.brendangregg.com/flamegraphs.html

Reads one or more files for profiling across multiple operations.

Allows summing across all clusters, nodes, indexes or shards.

Takes JSON input file names on command line, else reads a single file
from stdin.  Always outputs to stdout for piping to flamegraph.pl

Input data is expected to be JSON returned under "profile", when
"profile" is set to true in request.

Each "hit" == 1ns
Shards are shown as sNN
Nodes shown as generated node ids.

Thought of writing this in Perl for a femptosecond (since most
flamegraph tools are Perl), but I haven't started anything in Perl
recreationally in over 19 years...
"""

import argparse
import enum
import json
import os
import sys
from collections import Counter
from typing import Any, TextIO


class Detail(enum.Enum):
    NONE = 0  # don't show shards
    SHARD = 1  # don't show indices
    INDEX = 2  # don't show nodes
    NODE = 3  # don't show clusters
    CLUSTER = 4  # show clusters, nodes, indices, shards


class CollapseESPerf:
    def __init__(self, detail: Detail, use_descr: bool, breakdown: bool):
        self.detail = detail
        self.use_descr = use_descr
        self.breakdown = breakdown

        self.hits: Counter[str] = Counter()
        self.stack: list[str] = []

    def _push(self, name: str) -> None:
        self.stack.append(name)

    def _pop(self) -> None:
        self.stack.pop()

    def _record_node_and_pop(self, node: dict[str, Any]) -> None:
        if self.breakdown and "breakdown" in node:
            for name, nanos in node["breakdown"].items():
                if nanos:
                    self._push(name)
                    self._record_nanos_and_pop(nanos)  # pops stack
            self._pop()
        else:
            self._record_nanos_and_pop(node["time_in_nanos"])

    def _record_nanos_and_pop(self, nanos: int) -> None:
        key = ";".join(self.stack)
        self.hits[key] += nanos
        self._pop()

    def _query(self, qn: dict[str, Any]) -> None:
        # each query node has:
        # type, description, time_in_nanos, breakdown, children.

        if self.use_descr:
            # Just truncate description for now (whole thing
            # causes browser heartburn), and contains both fields
            # AND values which creates noise.  Need to sanitize so
            # field names present, but values are not!
            self._push(qn["description"][:100])
        else:
            # gives VERY dry output (just node types)
            self._push(qn["type"])

        for child in qn.get("children", []):
            self._query(child)

        self._record_node_and_pop(qn)
        # end of query

    def _coll(self, cn: dict[str, Any]) -> None:
        # collector nodes have: name, reason, time_in_nanos, children

        # reason is "plain english" description of class name
        # name is class name?
        self._push(cn["reason"])

        for child in cn.get("children", []):
            self._coll(child)

        self._record_node_and_pop(cn)
        # end of coll

    def _aggs(self, an: dict[str, Any]) -> None:
        # aggregations nodes have: type, description (agg name)
        #    time_in_nanos, breakdown, debug

        self._push(an["description"])  # aggregation name

        for child in an.get("children", []):
            self._aggs(child)

        self._record_node_and_pop(an)
        # end of aggs

    def collapse(self, stream: TextIO) -> None:
        p = json.load(stream)

        # take raw query response
        if "took" in p and "profile" in p:
            p = p["profile"]

        # top level has "shards", each of which has:
        # id (concatenated node_id, index, shard number)
        # node_id
        # shard_id
        # index
        # cluster

        self.stack = []
        for shard in p["shards"]:

            # prepare the foundation:
            if detail.value >= Detail.CLUSTER.value:
                self._push(shard["cluster"])
            if detail.value >= Detail.NODE.value:
                self._push(shard["node_id"])
            if detail.value >= Detail.INDEX.value:
                self._push(shard["index"])
            if detail.value >= Detail.SHARD.value:
                self._push(f"s{shard['shard_id']}")  # format shard as sNN

            self._push("search")
            for search in shard["searches"]:  # list
                # dict with 'query', 'rewrite_time', 'collector', 'aggregations'

                # maybe prepend digit to rewrite_time, coll, aggs to force order?
                self._push("rewrite")
                self._record_nanos_and_pop(search["rewrite_time"])

                self._push("query")
                for q in search["query"]:  # list
                    self._query(q)
                self._pop()  # "query"

                self._push("collector")
                for cn in search["collector"]:  # list
                    self._coll(cn)
                self._pop()  # "collector"
            self._pop()  # "search"

            self._push("aggregations")
            for an in shard.get("aggregations", []):  # list
                self._aggs(an)
            self._pop()  # "aggregations"

    def dump(self, output: TextIO) -> None:
        for key, sum in self.hits.items():
            output.write(f"{key} {sum}\n")


ap = argparse.ArgumentParser(
    "esperf-collapse",
    description="prepare Elasticsearch 'profile' data for flamegraph.pl",
)

levels = [level.name.lower() for level in Detail]
ap.add_argument("--detail", choices=levels, default="cluster", help="level of detail")
ap.add_argument(
    "--descr",
    action="store_true",
    default=False,
    help="display (truncated) description; may contain query data!",
)
ap.add_argument(
    "--no-breakdown",
    dest="breakdown",
    action="store_false",
    default=True,
    help="report breakdown times",
)
ap.add_argument(
    "--output", "-o", metavar="OUTPUT_FILE", dest="output", help="set output filename"
)
ap.add_argument("files", nargs="*", default=None)

args = ap.parse_args()
detail = getattr(Detail, args.detail.upper())

if args.descr and os.environ.get("ESPERF_NO_WARNING", None) in (None, ""):
    sys.stderr.write("WARNING! graphs may reveal query parameters!\n")

c = CollapseESPerf(detail, args.descr, args.breakdown)

if args.files:
    for fname in args.files:
        with open(fname) as f:
            c.collapse(f)
else:
    # read a single file from stdin
    c.collapse(sys.stdin)

if args.output:
    with open(args.output, "w") as f:
        c.dump(f)
else:
    c.dump(sys.stdout)
