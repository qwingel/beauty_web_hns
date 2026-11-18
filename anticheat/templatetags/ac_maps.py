from django import template

register = template.Library()
MAP_SCREENSHOTS = {
    "hns_floppytown": "community/hns_floppytown.jpg",
    "hns_mie": "community/hns_mie.jpg",
    "hns_tyo": "community/hns_tyo.jpg",
}


@register.filter
def map_screenshot(map_name):
    return MAP_SCREENSHOTS.get(map_name.lower(), "")
