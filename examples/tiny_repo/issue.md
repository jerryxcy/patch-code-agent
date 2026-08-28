# Incorrect discount calculation

`discounted_total` subtracts the decimal discount directly instead of applying it
to the subtotal. Make the failing test pass without changing its public API.

