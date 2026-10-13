<?php
/**
 * SDLC_OdooConnector: free companion module of the SDLC Odoo Magento 2 connector.
 *
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

use Magento\Framework\Component\ComponentRegistrar;

ComponentRegistrar::register(ComponentRegistrar::MODULE, 'SDLC_OdooConnector', __DIR__);
