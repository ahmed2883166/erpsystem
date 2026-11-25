-- Keep inventory movement unit references consistent with the unit master.
-- NOT VALID preserves legacy rows during upgrade while enforcing the FK for new writes.
DO $nexus$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_stock_movements_entered_uom'
          AND conrelid = 'stock_movements'::regclass
    ) THEN
        ALTER TABLE stock_movements
            ADD CONSTRAINT fk_stock_movements_entered_uom
            FOREIGN KEY (entered_uom_id)
            REFERENCES stock_uoms(id)
            NOT VALID;
    END IF;
END
$nexus$;
