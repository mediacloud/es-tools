"""
Top Elasticsearch tasks display
with decode for Mediacloud project
(only one index, limited set of queries)
"""

from typing import cast

from es_top import JSON, ESTop, get_path


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
        if query_string:
            # here with news-search-api DSL???
            # try to extract user query_string
            # XXX extract date range, source count!!!
            # (and return? or format??)

            # try to extract MC user query_string, number of sources,
            # date range HIGHLY sensitive to query construction!!!
            # works for MC, for now!!!
            nsrc = 0  # number of sources
            if " AND ((" in query_string:
                # here with likely web-search simple query
                query_string, rest = query_string.split(" AND ((", 1)

                sources, dates = rest.rsplit("AND publication_date:", 1)

                # quick try at getting number of sources:
                # does not handle url_search_strings!!!
                nsrc = len(sources.split(" OR "))

                if query_string.startswith("(("):
                    query_string = query_string[2:]
            else:
                if "AND publication_date:" in query_string:
                    # need to be more careful here??
                    query_string, dates = query_string.rsplit(
                        "AND publication_date:", 1
                    )

            return query_string, dates, nsrc

        # ES/DSL based provider
        # XXX extract date range, source count
        qs = get_path(j, "query.bool.must.query_string.query", None)
        return qs, "", 0

    def format_search_request(self, j: JSON, dsl: str, index: str) -> str:
        query_str, dates, nsrcs = self.extract_query_string(j)

        if not query_str:
            query_str = dsl

        query_str = query_str.replace("\n", " ")

        if nsrcs:
            query_str = f"{{{nsrcs}}} {query_str}"

        if dates:
            # skip brackets
            dates = dates[1:-1].replace(" TO ", ":")
            query_str = f"{dates} {query_str}"

        aggs = cast(JSON, j.get("aggregations"))
        if (
            aggs
            and aggs.get("dailycounts")
            and aggs.get("topdomains")
            and aggs.get("toplangs")
        ):
            query_str = f"OV: {query_str}"  # overview
        elif get_path(aggs, "sample.aggregations.topterms", None):
            query_str = f"TT: {query_str}"  # "top terms"
        elif j.get("size", 0) > 10:  # download?
            query_str = f"DL: {query_str}"
        elif j.get("size") != 0:  # leave importer checks alone
            query_str = f"OTHER: {query_str}"

        return query_str


if __name__ == "__main__":
    est = MCESTop()
    est.main()
