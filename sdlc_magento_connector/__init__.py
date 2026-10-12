from . import models
from . import services
from . import wizard


def pre_init_hook(env_or_cr):
    cr = getattr(env_or_cr, "cr", env_or_cr)
    # On fresh installs the table does not exist yet; guard migration SQL accordingly.
    cr.execute("""
        DO $$
        BEGIN
            IF to_regclass('public.magento_product_map') IS NULL THEN
                RETURN;
            END IF;

            ALTER TABLE magento_product_map
            DROP CONSTRAINT IF EXISTS magento_product_map_attribute_set_id_fkey;

            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'magento_product_map'
                  AND column_name = 'attribute_set_id'
                  AND data_type IN ('character varying', 'text')
            ) THEN
                EXECUTE $sql$
                    UPDATE magento_product_map
                    SET attribute_set_id = NULL
                    WHERE attribute_set_id IS NOT NULL
                      AND attribute_set_id !~ '^[0-9]+$'
                $sql$;
            END IF;
        END $$;
    """)
