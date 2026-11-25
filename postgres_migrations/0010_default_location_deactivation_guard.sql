-- Prevent deactivating a storage location while products still use it as default.
-- This preserves the same active-location invariant enforced when assigning a default.
CREATE OR REPLACE FUNCTION nexus_prevent_default_location_deactivation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $nexus_default_location$
BEGIN
    IF OLD.active = 1 AND NEW.active = 0
       AND EXISTS (
           SELECT 1
           FROM stock_product_locations AS product_location
           WHERE product_location.location_id = OLD.id
       ) THEN
        RAISE EXCEPTION
            'Cannot deactivate a location that is still assigned as a product default location.'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$nexus_default_location$;

DROP TRIGGER IF EXISTS trg_stock_locations_prevent_default_deactivation
    ON stock_locations;
CREATE TRIGGER trg_stock_locations_prevent_default_deactivation
BEFORE UPDATE OF active ON stock_locations
FOR EACH ROW
EXECUTE FUNCTION nexus_prevent_default_location_deactivation();
