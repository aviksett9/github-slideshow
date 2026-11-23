"""Extract one expiry's option risk arrays per symbol from an NSE Clearing SPAN 4.0 XML file."""
import json
import xml.etree.ElementTree as ET


def parse_span(src, out, target):
    """target: expiry as YYYYMMDD. Writes {created, target, phy, fut, opt} to `out`; returns the file's creation stamp."""
    res = {"created": None, "target": target, "phy": {}, "fut": {}, "opt": {}}
    for _, el in ET.iterparse(src, events=("end",)):
        tag = el.tag
        if tag == "created" and res["created"] is None:
            res["created"] = el.text
        elif tag == "phyPf":
            code = el.findtext("pfCode")
            p = el.find("phy")
            if p is not None:
                res["phy"][code] = {"p": float(p.findtext("p")), "priceScan": float(p.find("scanRate").findtext("priceScan"))}
            el.clear()
        elif tag == "futPf":
            code = el.findtext("pfCode")
            for f in el.findall("fut"):
                if f.findtext("pe") == target:
                    res["fut"][code] = {"p": float(f.findtext("p")),
                                        "priceScan": float(f.find("scanRate").findtext("priceScan"))}
            el.clear()
        elif tag == "oopPf":
            code = el.findtext("pfCode")
            for s in el.findall("series"):
                if s.findtext("pe") != target:
                    continue
                d = {}
                for o in s.findall("opt"):
                    ra = o.find("ra")
                    d[f'{o.findtext("o")}{float(o.findtext("k")):g}'] = {
                        "p": float(o.findtext("p")), "d": float(o.findtext("d")), "v": float(o.findtext("v")),
                        "ra": [float(a.text) for a in ra.findall("a")]}
                res["opt"][code] = d
            el.clear()
    with open(out, "w") as f:
        json.dump(res, f)
    return res["created"]
