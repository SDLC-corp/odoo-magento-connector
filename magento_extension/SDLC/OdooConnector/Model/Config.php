<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Model;

use Magento\Framework\App\Config\ScopeConfigInterface;
use Magento\Framework\Encryption\EncryptorInterface;

/**
 * Module settings (default scope).
 */
class Config
{
    private const PREFIX = 'sdlc_odoo_connector/';

    /** Outbox entity type => events/* flag. */
    private const EVENT_FLAGS = [
        Outbox::ENTITY_ORDER => 'orders',
        Outbox::ENTITY_INVOICE => 'invoices',
        Outbox::ENTITY_SHIPMENT => 'shipments',
        Outbox::ENTITY_CREDITMEMO => 'creditmemos',
        Outbox::ENTITY_PRODUCT => 'products',
        Outbox::ENTITY_CUSTOMER => 'customers',
        Outbox::ENTITY_STOCK => 'stock',
    ];

    /**
     * @param ScopeConfigInterface $scopeConfig
     * @param EncryptorInterface $encryptor
     */
    public function __construct(
        private readonly ScopeConfigInterface $scopeConfig,
        private readonly EncryptorInterface $encryptor
    ) {
    }

    /**
     * Is the module switched on (and configured enough to send)?
     *
     * @return bool
     */
    public function isEnabled(): bool
    {
        return $this->scopeConfig->isSetFlag(self::PREFIX . 'general/enabled')
            && $this->getEndpointUrl() !== '';
    }

    /**
     * Should saves of this entity type be recorded?
     *
     * @param string $entityType
     * @return bool
     */
    public function isEventEnabled(string $entityType): bool
    {
        $flag = self::EVENT_FLAGS[$entityType] ?? null;
        return $flag !== null && $this->isEnabled()
            && $this->scopeConfig->isSetFlag(self::PREFIX . 'events/' . $flag);
    }

    /**
     * Odoo webhook URL (https://odoo.example.com/magento/webhook/<key>).
     *
     * @return string
     */
    public function getEndpointUrl(): string
    {
        return trim((string)$this->scopeConfig->getValue(self::PREFIX . 'general/endpoint_url'));
    }

    /**
     * Decrypted shared secret.
     *
     * etc/config.xml declares the Encrypted backend model on this path, so the config
     * reader already returns it decrypted; a still-encrypted value ("<key>:<version>:...",
     * e.g. from a custom config source) is decrypted here.
     *
     * @return string
     */
    public function getSecret(): string
    {
        $value = trim((string)$this->scopeConfig->getValue(self::PREFIX . 'general/secret'));
        if ($value !== '' && preg_match('/^\d+:\d+:/', $value)) {
            $value = trim((string)$this->encryptor->decrypt($value));
        }
        return $value;
    }

    /**
     * Optional X-Odoo-Database header value.
     *
     * @return string
     */
    public function getOdooDatabase(): string
    {
        return trim((string)$this->scopeConfig->getValue(self::PREFIX . 'general/odoo_database'));
    }

    /**
     * @return int
     */
    public function getBatchSize(): int
    {
        return $this->intValue('delivery/batch_size', 100, 1, 500);
    }

    /**
     * @return int
     */
    public function getMaxAttempts(): int
    {
        return $this->intValue('delivery/max_attempts', 10, 1, 50);
    }

    /**
     * @return int
     */
    public function getTimeout(): int
    {
        return $this->intValue('delivery/timeout', 10, 1, 60);
    }

    /**
     * @return bool
     */
    public function verifySsl(): bool
    {
        $value = $this->scopeConfig->getValue(self::PREFIX . 'delivery/verify_ssl');
        return $value === null || (bool)$value;
    }

    /**
     * @return int
     */
    public function getRetentionDays(): int
    {
        return $this->intValue('delivery/retention_days', 7, 1, 365);
    }

    /**
     * Bounded integer setting.
     *
     * @param string $path
     * @param int $default
     * @param int $min
     * @param int $max
     * @return int
     */
    private function intValue(string $path, int $default, int $min, int $max): int
    {
        $value = $this->scopeConfig->getValue(self::PREFIX . $path);
        if ($value === null || $value === '' || !is_numeric($value)) {
            return $default;
        }
        return max($min, min($max, (int)$value));
    }
}
