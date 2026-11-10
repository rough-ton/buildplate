# Parts inventory

Keep track of the hardware you have on hand (screws, heat-set inserts, nuts, washers, magnets, bearings, standoffs and so on) on the **Parts** page. When a design needs hardware, the design agent can design around what you have, or pick standard parts and give you a shopping list.

## Adding parts

- **Add from catalog** lists over 100 common parts by category: metric screws (M2 to M5, socket head, button head and countersunk), heat-set inserts, hex and nyloc nuts, washers, disc magnets, ball bearings, standoffs and a few motion parts. Enter how many you have and select **Add**. Adding a part you already have adds to its quantity.
- **Custom part** is for anything not in the catalog. Give it a name, and optionally a category, size, links and notes.
- **Import CSV** merges a spreadsheet into your inventory (rows match by id, then catalog key, then name; others are added). **Export CSV** downloads your inventory in the same format, so the easiest way to make a CSV is to export one first.

Use **−** and **+**, or type a number, to change how many you have. Set **Warn when at or below** on a part to have it highlighted as **Low** when stock runs down.

Every catalog part carries the hole and pocket sizes the agent needs to model it: clearance and pilot holes, counterbores and countersinks for screws, hole size and depth for heat-set inserts, nut traps, magnet pockets and bearing seats. The agent uses these exact sizes, so parts fit.

## Hardware in Studio

The **Hardware** menu under the Studio prompt decides how a request handles hardware:

| Choice | What the agent does |
| --- | --- |
| **Ask me** | When your request clearly needs hardware (screws, inserts, magnets, bearings, hinges, wall mounting...), Buildplate asks which of the three below to use before spending any tokens. Otherwise it prefers parts you have. |
| **Use parts I have** | Uses only parts in your inventory, and no more than you have. A design that needs anything else is not saved until the agent changes it. |
| **Buy parts as needed** | Picks the best standard parts for the job, preferring ones you have, and lists what to buy. |
| **No hardware** | Uses printed alternatives: snap fits, printed threads, press-fit pins, living hinges. If hardware is truly unavoidable, the agent explains why. |

Follow-up requests on a design keep the choice you made for it. Set the default in **Settings > Parts and shopping**.

## The parts list

The **Parts** tab next to the viewer lists every part the current version uses, how many you have, and what to buy, with links to the Bambu Lab Store and Amazon. Library cards show how many parts a design uses and how many kinds you need to buy.

When you have printed and assembled a design, select **Mark as built** to take its parts out of your inventory. Buildplate remembers exactly what it took, so **Undo** puts them back. Inventory never changes on its own.

## Buy links

Buy links search the Bambu Lab Store for your region and your Amazon site. Set both in **Settings > Parts and shopping**, along with an optional **Amazon associate tag** that is added to Amazon links if you have an Amazon Associates account. To always use a particular product or shop for a part, edit the part and paste your own link; it replaces the search.

## MCP

MCP clients can use `list_parts`, `add_part` and `update_part_quantity`. `generate_design` takes `hardware_mode` (`ask`, `inventory`, `order` or `none`) and returns the bill of materials; with `ask`, a request that needs hardware returns a question to put to the user. `get_design` includes the bill of materials. See [MCP integration](mcp.md).
