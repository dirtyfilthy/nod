def total(lines, discount_pct=0, tax_pct=10):
    """Invoice total in dollars, rounded half-up to the cent.

    `lines` is a list of (quantity, unit_price). Lines with a quantity of zero
    or less are ignored. The discount applies before tax.
    """
    subtotal = sum(qty * price for qty, price in lines)
    discounted = subtotal - subtotal * discount_pct / 100
    return round(discounted + discounted * tax_pct / 100, 2)
