"""
Top Elasticsearch tasks display
with decode for Mediacloud project
(only one index, limited set of queries)
"""

from typing import cast

from es_top import JSON, ESTop, SearchRequest, get_path


def count_sources(qs: str) -> int:
    """
    take sources filter query_string
    and get a quick count of sources
    does not handle url_search_strings
    (will double count url:(http:.... OR https:...))
    """
    return len(qs.split(" OR "))


class MCESTop(ESTop):
    """
    ES top query display, with Media Cloud decode
    """

    def format_index_request(self, j: JSON, doc: str, index: str, _id: str) -> str:
        if "url" not in j:
            return doc  # XXX or ""
        url = j["url"]
        return f"importing {url} ({len(doc)} bytes)"

    def extract_query_string(self, j: JSON) -> tuple[str, str, int]:
        """
        returns query, date range, sources
        """

        # HIGHLY sensitive to query construction!!!
        # works for MC, for now!!!
        if j.get("size") == 0:
            id = get_path(j, "query.bool.filter.0.term._id.value", None)
            if id:
                return f"importer id check {id}", "", 0

        query_string = get_path(j, "query.query_string.query", None)
        dates = ""
        if query_string:
            # here with news-search-api DSL
            # try to extract MC user query_string, number of sources,
            # date range; HIGHLY sensitive to query construction!!!
            nsrc = 0  # number of sources
            if " AND ((" in query_string:
                # here with likely web-search simple query
                query_string, rest = query_string.split(" AND ((", 1)
                # XXX remove trailing "))"??

                sources, dates = rest.rsplit("AND publication_date:", 1)
                nsrc = count_sources(sources)

                if query_string.startswith("(("):
                    query_string = query_string[2:]
                    # eliminate trailing "))" too?
            else:
                if "AND publication_date:" in query_string:
                    # need to be more careful here??
                    query_string, dates = query_string.rsplit(
                        "AND publication_date:", 1
                    )

            return query_string, dates, nsrc

        # XXX extract date range, source count
        query_string = get_path(j, "query.bool.must.0.query_string.query", None)
        filters = get_path(j, "query.bool.filter", None)
        dates = ""
        nsrc = 0

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
                    nsrc = count_sources(dqs)

        return query_string, dates, nsrc

    def format_search_request(self, sr: SearchRequest) -> str:
        request = sr.dsl_json
        query_str, dates, nsrcs = self.extract_query_string(request)

        if not query_str:
            query_str = sr.dsl_text

        query_str = query_str.replace("\n", " ")

        # XXX include sr.preference (user/session) if not empty?

        if nsrcs:
            query_str = f"{{{nsrcs}}} {query_str}"

        if dates:
            # skip brackets
            dates = dates[1:-1].replace(" TO ", ":")
            query_str = f"{dates} {query_str}"

        aggs = cast(JSON, request.get("aggregations") or request.get("aggs"))
        if aggs:
            if (
                aggs.get("dailycounts")
                and aggs.get("topdomains")
                and aggs.get("toplangs")
            ):
                return f"OV: {query_str}"  # overview

            if get_path(aggs, "sample.aggregations.topterms", None):
                return f"TT: {query_str}"  # news-search-api "top terms"

            return f"AGG: {query_str}"  # something else with aggregations?

        size = request.get("size", 0)
        if size > 10:
            src = request.get("_source", [])
            if (
                isinstance(src, list)
                and len(src) == 2
                and "article_title" in src
                and "language" in src
            ):
                return f"TT: {query_str}"  # ES provider words
            return f"DL: {query_str}"

        if size == 0:  # leave importer checks alone
            return query_str

        return f"UNK: {query_str}"


if __name__ == "__main__":
    est = MCESTop()
    est.main()
