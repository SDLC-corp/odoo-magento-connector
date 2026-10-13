<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Plugin;

use Magento\Sales\Api\Data\CreditmemoCommentCreationInterface;
use Magento\Sales\Api\Data\CreditmemoCreationArgumentsInterface;
use Magento\Sales\Api\RefundInvoiceInterface;
use SDLC\OdooConnector\Model\ReturnToStockContext;

/**
 * POST /V1/invoice/{id}/refund: remember arguments.extension_attributes.return_to_stock_items
 * while the credit memo is saved.
 */
class RefundInvoiceReturnToStockPlugin
{
    /**
     * @param ReturnToStockContext $context
     */
    public function __construct(private readonly ReturnToStockContext $context)
    {
    }

    /**
     * @param RefundInvoiceInterface $subject
     * @param callable $proceed
     * @param int $invoiceId
     * @param array $items
     * @param bool $isOnline
     * @param bool $notify
     * @param bool $appendComment
     * @param CreditmemoCommentCreationInterface|null $comment
     * @param CreditmemoCreationArgumentsInterface|null $arguments
     * @return mixed
     * @SuppressWarnings(PHPMD.UnusedFormalParameter)
     * @SuppressWarnings(PHPMD.BooleanArgumentFlag)
     */
    public function aroundExecute(
        RefundInvoiceInterface $subject,
        callable $proceed,
        $invoiceId,
        array $items = [],
        $isOnline = false,
        $notify = false,
        $appendComment = false,
        ?CreditmemoCommentCreationInterface $comment = null,
        ?CreditmemoCreationArgumentsInterface $arguments = null
    ) {
        $this->context->set(ReturnToStockArguments::itemIds($arguments));
        try {
            return $proceed($invoiceId, $items, $isOnline, $notify, $appendComment, $comment, $arguments);
        } finally {
            $this->context->clear();
        }
    }
}
