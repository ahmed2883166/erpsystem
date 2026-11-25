-- Keep PostgreSQL product-location rules aligned with the SQLite inventory workflow.
CREATE OR REPLACE FUNCTION nexus_validate_active_product_location()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $nexus_location$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM stock_locations AS location
        WHERE location.id = NEW.location_id
          AND location.warehouse_id = NEW.warehouse_id
          AND location.active = 1
    ) THEN
        RAISE EXCEPTION
            'Default product location must be active and belong to the selected warehouse.'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$nexus_location$;

DROP TRIGGER IF EXISTS trg_stock_product_locations_active_insert
    ON stock_product_locations;
CREATE TRIGGER trg_stock_product_locations_active_insert
BEFORE INSERT ON stock_product_locations
FOR EACH ROW
EXECUTE FUNCTION nexus_validate_active_product_location();

DROP TRIGGER IF EXISTS trg_stock_product_locations_active_update
    ON stock_product_locations;
CREATE TRIGGER trg_stock_product_locations_active_update
BEFORE UPDATE OF warehouse_id, location_id ON stock_product_locations
FOR EACH ROW
EXECUTE FUNCTION nexus_validate_active_product_location();
