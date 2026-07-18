# Claims — pluto

이 파일은 `pluto` 토픽의 주장 묶음입니다. 각 주장은 독립 레코드입니다.
(주장이 많아지면 한 주장당 한 파일로 분리하세요.)

---

```yaml
id: clm-2026-0001
statement: "명왕성은 1930년 2월 18일 클라이드 톰보가 발견했다."
topic: pluto
type: event
sources:
  - ref: 20-raw/pluto-tombaugh-1930.md
    quote: "Pluto was discovered on 18 February 1930 by Clyde Tombaugh"
    locator: "¶1"
confidence: high
valid_from: 1930-02-18
valid_until: null
status: active
supersedes: null
superseded_by: null
contradicts: []
relations: []
created: 2026-06-19
updated: 2026-06-19
note: null
```

---

```yaml
id: clm-2026-0002
statement: "명왕성은 태양계의 아홉 번째 행성이다."
topic: pluto
type: claim
sources:
  - ref: 20-raw/pluto-tombaugh-1930.md
    quote: "it was announced as the ninth planet of the Solar System"
    locator: "¶1"
confidence: high
valid_from: 1930-02-18
valid_until: 2006-08-24
status: superseded
supersedes: null
superseded_by: clm-2026-0003
contradicts: []
relations: []
created: 2026-06-19
updated: 2026-06-19
note: "2006 IAU 결의로 행성 지위 상실. 덮어쓰지 않고 보존 — /timeline 용."
```

---

```yaml
id: clm-2026-0003
statement: "명왕성은 왜소행성(dwarf planet)이다."
topic: pluto
type: claim
sources:
  - ref: 20-raw/pluto-iau-2006.md
    quote: "Pluto ... was therefore reclassified as a dwarf planet."
    locator: "¶2"
confidence: high
valid_from: 2006-08-24
valid_until: null
status: active
supersedes: clm-2026-0002
superseded_by: null
contradicts: []
relations: ["caused_by:clm-2026-0004"]
created: 2026-06-19
updated: 2026-06-19
note: null
```

---

```yaml
id: clm-2026-0004
statement: "IAU는 2006년 8월 24일 행성 정의 결의 B5를 채택했고, 행성은 궤도 주변을 청소해야 한다는 기준을 포함한다."
topic: pluto
type: event
sources:
  - ref: 20-raw/pluto-iau-2006.md
    quote: "have cleared the neighbourhood around its orbit"
    locator: "¶1"
confidence: high
valid_from: 2006-08-24
valid_until: null
status: active
supersedes: null
superseded_by: null
contradicts: []
relations: []
created: 2026-06-19
updated: 2026-06-19
note: null
```
