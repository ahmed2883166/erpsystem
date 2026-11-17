-- Initialize carrying value for stock layers created by the operational receipt flow.
-- The SQLite workflow inserts qty_in and unit_cost and relies on the database default
-- for value_remaining. Keep that behavior safe on PostgreSQL without rewriting old layers.
CREATE OR REPLACE FUNCTION nexus_initialize_stock_layer_value()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $nexus_stock_value$
BEGIN
    IF NEW.value_remaining = 0
       AND NEW.qty_in > 0
       AND NEW.unit_cost > 0 THEN
        NEW.value_remaining := NEW.qty_in * NEW.unit_cost;
    END IF;
    RETURN NEW;
END;
$nexus_stock_value$;

DROP TRIGGER IF EXISTS trg_stock_layer_initialize_value ON stock_valuation_layers;
CREATE TRIGGER trg_stock_layer_initialize_value
BEFORE INSERT ON stock_valuation_layers
FOR EACH ROW
EXECUTE FUNCTION nexus_initialize_stock_layer_value();
