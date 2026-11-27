# Synthetic Companion JATS XML Example (Illustrative Only)

> Note: Harbor Skill bundles only allow standard text resource extensions (`.md`, `.json`, `.py`, `.txt`), while runtime evaluation `/input/` supplies real `.xml` files directly. Below is an illustrative `secondary-publisher.xml` structure supported by `scripts/corpus_ingest.py` (`benchmark_suite.py` also generates and tests a live temporary `.xml` file during verification).

```xml
<?xml version="1.0" encoding="UTF-8"?>
<article>
  <front>
    <journal-meta><journal-title-group><journal-title>Synthetic Reaction Engineering</journal-title></journal-title-group></journal-meta>
    <article-meta>
      <article-id pub-id-type="doi">10.9999/synth.secondary.2026</article-id>
      <title-group><article-title>Synthetic Cobalt-Promoted CO2 Hydrogenation Under Low-Pressure Flow</article-title></title-group>
      <contrib-group>
        <contrib contrib-type="author"><name><surname>Gomez</surname><given-names>Maria</given-names></name></contrib>
        <contrib contrib-type="author"><name><surname>Sato</surname><given-names>Kenji</given-names></name></contrib>
      </contrib-group>
      <pub-date pub-type="epub"><year>2026</year></pub-date>
    </article-meta>
  </front>
  <body>
    <sec id="sec_2"><title>2. Methods and Findings</title>
      <p id="sec2-p1">We synthesized sample Co-Ni-FlowB (5 wt% Co-10 wt% Ni/CeO<sub>2</sub>) and measured CO<sub>2</sub> conversion at 573 K and 0.5 bar with space velocity 10<sup>-5</sup> s<sup>-1</sup> equivalent residence rate.</p>
      <p id="sec2-p2">Co-Ni-FlowB achieved 79.1% CO<sub>2</sub> conversion with R<sup>2</sup> = 0.994 in kinetic fitting, whereas unpromoted Ni/Al<sub>2</sub>O<sub>3</sub> (commercial pellet Lot-Z) showed 61.0% conversion under 573 K and 0.5 bar.</p>
    </sec>
  </body>
</article>
```
