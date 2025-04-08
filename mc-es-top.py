"""
Top Elasticsearch tasks display
with decode for Mediacloud project
(only one index, limited set of queries)
"""

import re
from typing import cast

from es_top import JSON, ESTop, SearchRequest, get_path

_OUTSIDE_PARENS_RE = re.compile(r" OR (?![^(]*\))")
_CANDOM_PAREN = "canonical_domain:("
_URL_PREFIX = "url:("


def split_sources(qs: str) -> list[str]:
    """
    take sources filter query_string, returns list of sources
    """
    clauses = _OUTSIDE_PARENS_RE.split(qs)
    sources: list[str] = []
    if clauses:
        c0 = clauses[0]
        if c0.startswith(_CANDOM_PAREN) and c0.endswith(")"):
            clauses.pop(0)
            # remove prefix, closing paren and split
            sources = c0.removeprefix(_CANDOM_PAREN)[:-1].split(" OR ")

        # ES Provider allows two flavors:
        if len(clauses) == 1 and (c0 := clauses[0]).startswith(_URL_PREFIX):
            # url:(http://...* OR https://...* [ OR ...])
            # remove prefix and closing paren and split
            usss = c0.removeprefix(_URL_PREFIX)[:-1].split(" OR ")
            for uss in usss:
                if uss.startswith("http:"):
                    sources.append(uss.removeprefix("http:"))
        elif clauses:
            # (canonical_domain:X AND url:(http://...* OR https://...*)) [ OR ...]
            for clause in clauses:
                if (
                    clause.startswith("(canonical_domain:")
                    and clause.endswith("))")
                    and " AND " in clause
                    and " OR https://" in clause
                ):
                    z = clause.split(" OR https://")
                    if len(z) == 2:
                        sources.append(z[1][:-2])  # ignore trailing parens

    return sources


class MCESTop(ESTop):
    """
    ES top query display, with Media Cloud decode
    """

    def format_index_request(self, j: JSON, doc: str, index: str, _id: str) -> str:
        if "url" not in j:
            return doc  # XXX or ""
        url = j["url"]
        return f"importing {url} ({len(doc)} bytes)"

    def extract_query_string(self, j: JSON) -> tuple[str, str, list[str]]:
        """
        takes full DSL, returns user query, date range, sources

        HIGHLY sensitive to query construction!!!
        works for MC, for now!!!
        """

        if j.get("size") == 0:
            id = get_path(j, "query.bool.filter.0.term._id.value", None)
            if id:
                return f"importer id check {id}", "", []

        query_string = get_path(j, "query.bool.must.0.query_string.query", None)
        filters = get_path(j, "query.bool.filter", None)
        dates = ""
        srcs = []

        if filters and not query_string:
            query_string = "*"

        if query_string and filters:
            # elasticsearch_dsl based mc-providers:
            # {
            #   'bool': {
            #       'must': [{'query_string': {'query': 'user query string', ...}}],
            #       'filter': [filters....]
            #   }
            # }
            for filter in filters:
                # handle {range: {publication_date: {gte: "start", lte: "end"}}}
                start_date = get_path(filter, "range.publication_date.gte", None)
                end_date = get_path(filter, "range.publication_date.lte", None)
                if isinstance(start_date, str) and isinstance(end_date, str):
                    # make look like query_string for now:
                    dates = f"[{start_date[:10]} TO {end_date[:10]}]"
                    continue

                # {query_string: {query: 'canonical_domain:(nytimes.com)'}}
                dqs = get_path(filter, "query_string.query", None)
                # str.startswith and .endswith take iterables, but .find does not:
                if isinstance(dqs, str) and (
                    "canonical_domain:" in dqs or "url:" in dqs
                ):
                    srcs = split_sources(dqs)

        return query_string, dates, srcs

    def format_search_request(self, sr: SearchRequest) -> str:
        request = sr.dsl_json
        query_str, dates, srcs = self.extract_query_string(request)

        if not query_str:
            query_str = sr.dsl_text

        query_str = query_str.replace("\n", " ")
        out = [query_str]

        if sr.preference:
            out.insert(0, f"<{sr.preference}>")

        if srcs:
            if len(srcs) == 1:
                out.insert(0, f"{{{srcs[0]}}}")
            else:
                out.insert(0, f"{{{len(srcs)}}}")

        if dates:
            # skip brackets
            dates = dates[1:-1].replace(" TO ", ":")
            out.insert(0, dates)

        aggs = cast(JSON, request.get("aggregations") or request.get("aggs"))
        if aggs:
            if (
                aggs.get("dailycounts")
                and aggs.get("topdomains")
                and aggs.get("toplangs")
            ):
                out.insert(0, "OV:")  # overview
            elif get_path(aggs, "sample.aggregations.topterms", None):
                out.insert(0, "OTT:")  # news-search-api "top terms"
            else:
                out.insert(0, "AGG:")  # something else with aggregations?
        else:
            size = request.get("size", 0)
            if size >= 10:
                src = request.get("_source", [])
                if "includes" in src:
                    src = src["includes"]
                must = get_path(request, "query.bool.must", [])
                if (
                    isinstance(must, list)
                    and len(must) > 1
                    and isinstance(
                        get_path(
                            must[1], "function_score.functions.0.random_score", None
                        ),
                        dict,
                    )
                ):
                    if (
                        isinstance(src, list)
                        and len(src) == 2
                        and "article_title" in src
                        and "language" in src
                    ):
                        out.insert(0, "TT:")  # top terms
                    elif isinstance(src, list) and len(src) == 7:
                        out.insert(0, "SPL:")  # sample
                    else:
                        out.insert(0, "RAND:")
                else:
                    out.insert(0, "DL:")  # download
            elif size > 0:  # leave importer checks alone
                out.insert(0, "UNK:")
        return " ".join(out)


if __name__ == "__main__":
    est = MCESTop()
    est.main()
