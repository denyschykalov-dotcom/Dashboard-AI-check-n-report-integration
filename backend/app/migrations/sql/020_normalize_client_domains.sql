-- Clean up client domains that were stored as a pasted URL.
--
-- New clients are normalized on save (see service.clean_domain), but rows
-- created before that may hold "https://acme.com" or "acme.com/". The report
-- builds every clickable page link as "https://" + this value, so such a row
-- produced "https://https://acme.com/page" — a dead link in a document a client
-- reads — and stopped the Ahrefs top-movers URLs being shortened to paths.
--
-- Strips the scheme, then everything from the first / ? or #, then surrounding
-- dots and spaces, and lowercases — the same steps as clean_domain(). "www." is
-- kept on purpose: it is part of the host that serves the site, and the link has
-- to reach it. Already-clean rows are rewritten to the same value, so running
-- this changes nothing that was right.
UPDATE "Dashboard_ReportBuilder_clients"
SET domain = lower(
        btrim(
            regexp_replace(
                regexp_replace(domain, '^[A-Za-z][A-Za-z0-9+.-]*://', ''),
                '[/?#].*$',
                ''
            ),
            '. '
        )
    )
WHERE domain IS NOT NULL;
