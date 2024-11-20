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
import io
import json
import os
import sys
from collections import Counter
from typing import Any, cast


class Detail(enum.Enum):
    NONE = 0  # don't show shards
    SHARD = 1  # don't show indices
    INDEX = 2  # don't show nodes
    NODE = 3  # don't show clusters
    CLUSTER = 4  # show clusters, nodes, indices, shards


hits: Counter[str] = Counter()


def collapse(stream: io.TextIOWrapper, detail: Detail, use_descr: bool = False) -> None:
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

    for shard in p["shards"]:
        stack = []

        def query(q: dict[str, Any]) -> None:
            # each query node has:
            # type, description, time_in_nanos, breakdown, children.

            if use_descr:
                # Just truncate description for now (whole thing
                # causes browser heartburn), and contains both fields
                # AND values which creates noise.  Need to sanitize so
                # field names present, but values are not!
                stack.append(q["description"][:100])
            else:
                # gives VERY dry output (just node types)
                stack.append(q["type"])

            for child in q.get("children", []):
                query(child)

            key = ";".join(stack)
            hits[key] += q["time_in_nanos"]
            stack.pop()
            # end of query

        if detail.value >= Detail.CLUSTER.value:
            stack.append(shard["cluster"])
        if detail.value >= Detail.NODE.value:
            stack.append(shard["node_id"])
        if detail.value >= Detail.INDEX.value:
            stack.append(shard["index"])
        if detail.value >= Detail.SHARD.value:
            # format shard as sNN
            # (keep it narrow, there may be lots of 'em!)
            stack.append(f"s{shard['shard_id']}")

        for search in shard["searches"]:  # list
            # dict with 'query', 'rewrite_time', 'collector'
            for q in search["query"]:  # list
                query(q)


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
ap.add_argument("-o", metavar="OUTPUT_FILE", dest="output", help="set output filename")
ap.add_argument("files", nargs="*", default=None)

args = ap.parse_args()
detail = getattr(Detail, args.detail.upper())

if args.descr and os.environ.get("ESPERF_NO_WARNING", None) in (None, ""):
    sys.stderr.write("WARNING! graphs may reveal query parameters!\n")

if args.files:
    for fname in args.files:
        with open(fname) as f:
            collapse(f, detail, args.descr)
else:
    # read a single file from stdin
    collapse(cast(io.TextIOWrapper, sys.stdin), detail, args.descr)  # ??

if args.output:
    output = open(args.output, "w")
else:
    output = cast(io.TextIOWrapper, sys.stdout)  # ??

for key, sum in hits.items():
    output.write(f"{key} {sum}\n")
