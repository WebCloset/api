from jinja2 import Template


def generate_group_ad_script(product):
    print(product)
    prices = [o[4] for o in product]
    lowest_price = min(prices) / 100
    highest_price = max(prices) / 100

    colors = sorted(set([o[6] for o in product if o[6]]))
    #conditions = sorted(set([o["condition"] for o in offers if o["condition"]]))
    currency = product[0][5]

    template_body = """
Looking for the {{ brand }} {{ title }} – Size {{ size }}?

Now available on WebCloset starting from just {{ lowest_price }} {{ currency }}.
Price range: {{ lowest_price }} - {{ highest_price }} {{ currency }}.

Available in {{ colors }}.

Compare multiple sellers instantly and grab the best deal today!
"""

    template = Template(template_body)

    return template.render(
        brand=product[0][0],
        title=product[0][1],
        size=product[0][2],
        lowest_price=lowest_price,
        highest_price=highest_price,
        colors=", ".join(colors),
        currency=currency
    )
