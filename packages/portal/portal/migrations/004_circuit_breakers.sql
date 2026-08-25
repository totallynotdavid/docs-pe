-- Fleet-wide circuit breaker state, keyed by (site, provider). Mirrors
-- fetch.pipeline.breaker's threshold and cooldown formula (see
-- portal/repository/breakers.py, which imports the constants rather than
-- redefining them) but lives in Postgres instead of one agent process's
-- memory: with N worker nodes each running their own in-process breaker,
-- a systemic failure needed ~10*N wasted attempts fleet-wide before every
-- lane actually stopped. This table is the shared backstop /claim filters
-- against, so ten consecutive failures anywhere park every node's lanes for
-- that pair, matching the single-process invariant docs/architecture.md
-- describes.

CREATE TABLE portal_circuit_breakers (
    source text NOT NULL REFERENCES portal_sites (code),
    provider text NOT NULL CONSTRAINT portal_circuit_breakers_provider_supported
        CHECK (provider IN ('geonode', 'dataimpulse')),

    consecutive_failures integer NOT NULL DEFAULT 0
        CHECK (consecutive_failures >= 0),
    level integer NOT NULL DEFAULT 0 CHECK (level >= 0),
    open_until timestamptz,

    updated_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (source, provider)
);

CREATE TRIGGER portal_circuit_breakers_set_updated_at
    BEFORE UPDATE ON portal_circuit_breakers
    FOR EACH ROW
    EXECUTE FUNCTION portal_set_updated_at();
