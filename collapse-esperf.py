"""
Phil Budne @ Media Cloud
Started October 23, 2024

Take Elasticsearch "profile" response section and output for
consumption by flamegraph.pl or https://pypi.org/project/flameshow/

See: https://github.com/brendangregg/FlameGraph
and https://www.brendangregg.com/flamegraphs.html

Reads one or more files for profiling across multiple operations.

Allows summing across all clusters, nodes, indexes or shards.

Takes JSON input file names on command line, else reads a single file
from stdin.  Always outputs to stdout for piping to flamegraph.pl

Input data is expected to be JSON returned under "profile", when
"profile" is set to true in request.

Each "sample" == 1ns
Shards are shown as sNN
Nodes shown as generated node ids.

Thought of writing this in Perl for a femptosecond (since most
flamegraph tools are Perl), but I haven't started anything in Perl
recreationally in over 19 years...
"""

import argparse
import json
import os
import sys
from collections import Counter
from typing import Any, TextIO


class LabelContext:
    def __init__(self, cperf: "CollapseESPerf", label: str):
        self.cperf = cperf
        self.label = label

    def __enter__(self) -> "LabelContext":
        self.cperf._push(self.label)
        return self

    def __exit__(self, type_: Any, value: Any, traceback: Any) -> None:
        self.cperf._pop()


class CollapseESPerf:
    def __init__(self, detail: str, use_descr: bool, breakdown: bool):
        # options, set for all files
        self.detail = detail  # string of c(luser), n(node), i(index), s(hard)
        self.use_descr = use_descr
        self.breakdown = breakdown

        # reset for each shard of a query
        self.stack: list[str] = []

        # summed over all shards in all queries in all files;
        # flamegraphs are usually done with time based sampling.
        # Here, each "sample" is in a reported nanosecond of runtime.
        self.samples: Counter[str] = Counter()

    def _reset_stack(self) -> None:
        self.stack = []

    def _push(self, name: str) -> None:
        self.stack.append(name)

    def _pop(self) -> None:
        self.stack.pop()

    def _record_node(self, node: dict[str, Any]) -> None:
        """
        record "samples" for a node with time_in_nanos
        OR possibly a "breakdown"
        """
        if self.breakdown and "breakdown" in node:
            for name, nanos in node["breakdown"].items():
                if nanos:
                    with self._label(name):
                        self._record_nanos(nanos)
        else:
            self._record_nanos(node["time_in_nanos"])

    def _record_nanos(self, nanos: int) -> None:
        # the one place that formats the stack
        key = ";".join(self.stack)
        self.samples[key] += nanos

    def _label(self, name: str) -> LabelContext:
        return LabelContext(self, name)

    def _query(self, qn: dict[str, Any]) -> None:
        # each query node has:
        # type, description, time_in_nanos, breakdown, children.

        if self.use_descr:
            # Just truncate description for now (whole thing
            # causes browser heartburn), and contains both fields
            # AND values which creates noise.  Need to sanitize so
            # field names present, but values are not!
            label = qn["description"][:100]
        else:
            # gives VERY dry output (just node types)
            label = qn["type"]

        with self._label(label):
            for child in qn.get("children", []):
                self._query(child)

            self._record_node(qn)  # record time_in_nanos or breakdown
        # end of _query

    def _coll(self, cn: dict[str, Any]) -> None:
        # collector nodes have: name, reason, time_in_nanos, children

        # reason is "plain english" description of class name
        # name is class name?
        with self._label(cn["reason"]):
            for child in cn.get("children", []):
                self._coll(child)
            self._record_node(cn)  # record time_in_nanos
        # end of _coll

    def _aggs(self, an: dict[str, Any]) -> None:
        # aggregations nodes have: type, description (agg name)
        #    time_in_nanos, breakdown, debug

        with self._label(an["description"]):  # aggregation name
            for child in an.get("children", []):
                self._aggs(child)
            self._record_node(an)  # record time_in_nanos or breakdown
        # end of _aggs

    def collapse(self, stream: TextIO) -> None:
        p = json.load(stream)

        # handle raw query response
        if "took" in p and "profile" in p:
            p = p["profile"]

        # top level has list "shards", each of which has:
        # id (concatenated node_id, index, shard number)
        # node_id
        # shard_id
        # index
        # cluster

        for shard in p["shards"]:
            self._reset_stack()

            # prepare the foundation, to order.
            # (should be the only place that calls _push directly).
            for x in self.detail:
                if x == "c":
                    self._push(shard["cluster"])
                elif x == "n":
                    self._push(shard["node_id"])
                elif x == "i":
                    self._push(shard["index"])
                elif x == "s":
                    self._push(f"s{shard['shard_id']}")  # format shard as sNN

            with self._label("search"):
                for search in shard["searches"]:  # list
                    # here with dict with 'query', 'rewrite_time',
                    # 'collector', 'aggregations'

                    with self._label("rewrite"):  # add digit?
                        self._record_nanos(search["rewrite_time"])

                    with self._label("query"):  # add digit?
                        for q in search["query"]:  # list
                            self._query(q)

                    with self._label("collector"):  # add digit?
                        for cn in search["collector"]:  # list
                            self._coll(cn)

            with self._label("aggregations"):  # add digit?
                for an in shard.get("aggregations", []):  # list
                    self._aggs(an)

    def dump(self, output: TextIO) -> None:
        for key, sum in self.samples.items():
            output.write(f"{key} {sum}\n")


ap = argparse.ArgumentParser(
    "esperf-collapse",
    description="prepare Elasticsearch 'profile' data for flamegraph.pl",
    epilog="""

-D takes a string of characters to use (in order) for breakdown at
the bottom (root) of the graph: c for cluster, n for node, i for
index, and s for shard. --detail takes a series of full words after
the option.  Using --detail more than once will override the previous
value(s).

If there are multiple indices in the query, selecting shards without,
or before indices will mix timings on shards from different indices.

If there is only one index, putting node before shard will show total
times across nodes (hot nodes), while putting shards first will show
hot shards.
""",
)

g = ap.add_mutually_exclusive_group(required=False)
g.add_argument(
    "-D", dest="detail_chars", default="", help="string with c, n, i, s (see below)"
)
g.add_argument(
    "--detail", choices=["cluster", "node", "index", "shard"], nargs="*", default=[]
)

ap.add_argument(
    "--descr",
    action="store_true",
    default=False,
    help="display (truncated) description rather than node type; may show query data!",
)
ap.add_argument(
    "--no-breakdown",
    dest="breakdown",
    action="store_false",
    default=True,
    help="omit luncene timing breakdown",
)
ap.add_argument(
    "--output",
    "-o",
    metavar="OUTPUT_FILE",
    dest="output",
    help="set output filename; defaults to stdout",
)
ap.add_argument("files", nargs="*", default=None)

args = ap.parse_args()

if args.descr and not os.environ.get("ESPERF_NO_WARNING", None):
    sys.stderr.write("WARNING! graphs may reveal query parameters!\n")

detail = ""
if args.detail_chars:
    seen = set()
    detail = args.detail_chars
    for x in detail:
        if x not in "cnis":
            sys.stderr.write(f"Unknown detail character '{x}'\n")
            sys.exit(1)
        if x in seen:
            sys.stderr.write(f"Duplicate detail character '{x}'\n")
            sys.exit(1)
        seen.add(x)
elif args.detail:  # list of strings
    for x in args.detail:
        c = x[0]
        if c in detail:
            sys.stderr.write(f"Duplicate detail string '{x}'\n")
            sys.exit(1)
        detail += c  # turn into detail_string

cesp = CollapseESPerf(detail, args.descr, args.breakdown)

if args.files:
    for fname in args.files:
        with open(fname) as f:
            cesp.collapse(f)
else:
    # read a single file from stdin
    cesp.collapse(sys.stdin)

if args.output:
    with open(args.output, "w") as f:
        cesp.dump(f)
else:
    cesp.dump(sys.stdout)
