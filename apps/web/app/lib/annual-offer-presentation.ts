import type { AnnualOffer, AnnualProvider } from './billing-v2'

/**
 * Presentation-only helpers for the signed annual offer.  The public client
 * never substitutes a customer class, price, interval, or provider list.
 */
function formatMinorAmount(amountMinor: number, currency: string): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency,
  }).format(amountMinor / 100)
}

export function annualOfferClassLabel(offer: AnnualOffer): string {
  return offer.customerClass === 'early' ? 'Early Adopter' : 'Standard'
}

export function annualOfferPlanLabel(offer: AnnualOffer): string {
  return `${annualOfferClassLabel(offer)} Plan`
}

export function formatAnnualOfferAmount(offer: AnnualOffer): string {
  return formatMinorAmount(offer.annualAmountMinor, offer.currency)
}

export function formatAnnualOfferMonthlyEquivalent(offer: AnnualOffer): string {
  return formatMinorAmount(offer.monthlyEquivalentMinor, offer.currency)
}

export function annualOfferAnnualLabel(offer: AnnualOffer): string {
  return `${formatAnnualOfferAmount(offer)}/year`
}

export function annualOfferMonthlyLabel(offer: AnnualOffer): string {
  return `${formatAnnualOfferMonthlyEquivalent(offer)}/month`
}

export function annualOfferRenewalCopy(offer: AnnualOffer): string {
  return `${annualOfferAnnualLabel(offer)}, billed annually (${annualOfferMonthlyLabel(offer)})`
}

export function isAnnualOfferProviderAvailable(
  offer: AnnualOffer,
  provider: AnnualProvider,
  globallyEnabled = true,
): boolean {
  return globallyEnabled && offer.providers.includes(provider)
}

export type AnnualOfferAnalyticsDimensions = {
  plan_id: AnnualOffer['planId']
  customer_class: AnnualOffer['customerClass']
  billing_interval: AnnualOffer['billingInterval']
  annual_amount_minor: AnnualOffer['annualAmountMinor']
  monthly_equivalent_minor: AnnualOffer['monthlyEquivalentMinor']
  currency: AnnualOffer['currency']
}

/** Deliberately excludes offerToken, requestId, and other linkable authority. */
export function annualOfferAnalyticsDimensions(offer: AnnualOffer): AnnualOfferAnalyticsDimensions {
  return {
    plan_id: offer.planId,
    customer_class: offer.customerClass,
    billing_interval: offer.billingInterval,
    annual_amount_minor: offer.annualAmountMinor,
    monthly_equivalent_minor: offer.monthlyEquivalentMinor,
    currency: offer.currency,
  }
}
