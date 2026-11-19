-- Add a selectable automatic/manual replenishment policy without changing existing thresholds.
ALTER TABLE stock_warehouse_product_settings
    ADD COLUMN IF NOT EXISTS reorder_mode TEXT NOT NULL DEFAULT 'auto'
    CHECK (reorder_mode IN ('auto','manual'));
