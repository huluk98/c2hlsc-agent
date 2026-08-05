"use strict";

(function exposeReadingCoach(globalObject) {
  const BLOCK_LABELS = Object.freeze({
    day1: "Day 1 / Preflight and Stages 1-4",
    day2: "Day 2 / Stages 5-8",
    day3: "Day 3 / Stages 9-12"
  });

  const TEXTBOOK_GUIDES = Object.freeze({
    day1: Object.freeze({
      title: "Read the Day 1 sections just in time",
      steps: Object.freeze([
        "Preflight: Chapter 1 Section 1.4 for representation and two's complement.",
        "Stages 1-2: gates, Boolean algebra, XOR, and the half-adder.",
        "Stages 3-4: mux coding, latch avoidance, width, and signedness."
      ]),
      artifact: "Produce the preflight answers, XOR, half-adder, two mux styles, latch correction, and width/sign worksheet.",
      gate: "MANUAL artifacts match their truth tables and boundary oracles; the LOCAL preflight environment check passes."
    }),
    day2: Object.freeze({
      title: "Read state before writing clocked RTL",
      steps: Object.freeze([
        "Stage 5: learn old-state/new-state reasoning and nonblocking assignment.",
        "Stage 6: separate registers, next-state logic, reset, and FSM outputs.",
        "Stages 7-8: use the local cycle trace and explicit ready/valid transfer rule."
      ]),
      artifact: "Produce E1-E3 state predictions, reset/FSM tables, an E0-E10 waveform explanation, and the E0-E4 handshake classification.",
      gate: "MANUAL for Stages 5, 6, and 8; LOCAL make sim plus quiz >= 8/10 for Stage 7."
    }),
    day3: Object.freeze({
      title: "Read for the RTL and HLS evidence boundary",
      steps: Object.freeze([
        "Stages 9-10: distinguish oracle, mutation, simulation, and synthesis evidence.",
        "Stage 11A: switch to HLStrans pages 1-2 for literature comprehension.",
        "Stages 11B-12: use documentation matching the installed HLS tool and target."
      ]),
      artifact: "Produce oracle/mutation evidence, annotated synthesis evidence, one literature-ledger row, and a filled capstone contract.",
      gate: "LOCAL make verify; MANUAL literature/capstone review; EXTERNAL only for preserved real HLS results."
    })
  });

  const TEXTBOOK_READINGS = Object.freeze({
    day1: Object.freeze([
      Object.freeze({
        title: "Gates, Boolean algebra, XOR, and half-adder",
        detail: "Read Chapter 1 Sections 1.4-1.5, Chapter 2 Sections 2.1-2.4, Chapter 4 Sections 4.1-4.2, and Chapter 5 Section 5.2.1 just before the matching exercises. Match section titles to your edition.",
        stage: "Preflight and Stages 1-2",
        artifact: "Conversion answers, XOR equation/gate sketch/RTL/exhaustive table, and half-adder sum/carry table with RTL.",
        gate: "MANUAL: all small input spaces and boundary answers agree with independent truth tables.",
        source: "Digital Design and Computer Architecture; owned edition, section-title mapping",
        url: null,
        kind: "reading",
        availability: "Available after confirming the selected PDF title and edition."
      }),
      Object.freeze({
        title: "Muxes, combinational RTL, width, and signedness",
        detail: "Read Chapter 2 Section 2.8.1; Chapter 4 Sections 4.2 and 4.5, especially 4.2.7 and 4.5.4; then the relevant Chapter 5 arithmetic section. Focus on complete assignments and expression sizing.",
        stage: "Stages 3-4",
        artifact: "Two equivalent muxes, one diagnosed/corrected latch, and the boundary-value width/sign worksheet.",
        gate: "MANUAL: mux truth tables match, no unintended latch remains, and every width choice is explained.",
        source: "Digital Design and Computer Architecture; owned edition, section-title mapping",
        url: null,
        kind: "reading",
        availability: "Available after confirming the selected PDF title and edition."
      })
    ]),
    day2: Object.freeze([
      Object.freeze({
        title: "Registers, edges, reset, and nonblocking assignment",
        detail: "Read Chapter 3 Sections 3.2-3.3 and Chapter 4 Sections 4.4 and 4.5.4. Predict every right-hand side from pre-edge state before applying updates.",
        stage: "Stage 5",
        artifact: "Exact E1-E3 (q1,q2) table plus an old-state/new-state explanation.",
        gate: "MANUAL: predictions use pre-edge values and agree with the clocked semantics exercise.",
        source: "Digital Design and Computer Architecture; owned edition, section-title mapping",
        url: null,
        kind: "reading",
        availability: "Available after confirming the selected PDF title and edition."
      }),
      Object.freeze({
        title: "FSM structure, waveform timing, and transactions",
        detail: "Read Chapter 3 Section 3.4, Chapter 4 Section 4.6, and Chapter 5 Section 5.4.1. Then switch to the local waveform walkthrough and field-guide transfer rule for Stages 7-8.",
        stage: "Stages 6-8",
        artifact: "Counter/FSM tables, predicted E0-E10 trace, and E0-E4 ready/valid classification.",
        gate: "MANUAL for FSM and handshake artifacts; LOCAL make sim and quiz >= 8/10 for the waveform.",
        source: "Textbook plus local waveform_walkthrough.md and fundamentals_field_guide.md",
        url: "waveform_walkthrough.md",
        kind: "reading",
        availability: "Textbook section plus a local study guide available from this page."
      })
    ]),
    day3: Object.freeze([
      Object.freeze({
        title: "Verification, testbenches, and synthesis boundaries",
        detail: "Read Chapter 4 Sections 4.8 and 4.1.3, then the field-guide evidence boundary and generation-contract oracle/QoR fields.",
        stage: "Stages 9-10",
        artifact: "Independent-oracle and mutation results plus annotated lint and synthesis evidence.",
        gate: "LOCAL: make exhaustive, make mutations, make lint, and make synth all pass.",
        source: "Textbook plus local fundamentals_field_guide.md and generation_contract.md",
        url: "fundamentals_field_guide.md",
        kind: "reading",
        availability: "Textbook section plus local evidence guides available from this page."
      }),
      Object.freeze({
        title: "Know where the textbook stops: HLS evidence",
        detail: "Use the local reading map to switch to HLStrans pages 1-2 for Stage 11A and exact-version vendor documentation for Stage 11B. Do not use the textbook as proof of a tool run.",
        stage: "Stages 11-12",
        artifact: "HLStrans evidence-ledger row and a capstone contract with real-tool fields explicitly available or unavailable.",
        gate: "MANUAL for literature/capstone reasoning; EXTERNAL only for preserved real HLS outputs.",
        source: "Local pdf_reading_map.md; HLStrans and vendor sources are separate from this textbook",
        url: "pdf_reading_map.md",
        kind: "reading",
        availability: "Local route available; the HLStrans PDF and tool-matched docs are separate sources."
      })
    ])
  });

  const HLSTRANS_READINGS = Object.freeze({
    day1: Object.freeze([
      Object.freeze({
        title: "Confirm the HLStrans identity, then defer it",
        detail: "Inspect only the title page and abstract. This source concerns C-to-HLS transformation, not the gate/Boolean prerequisites for Day 1.",
        stage: "Day 1 source routing; deferred to Stage 11A",
        artifact: "Record arXiv:2507.04315v3, date, title, and a note that the paper is deferred to Day 3.",
        gate: "MANUAL: title-page identity agrees; no Day 1 mastery credit is claimed from this paper.",
        source: "Selected HLStrans PDF, title page and abstract",
        url: null,
        kind: "reading",
        availability: "Available in the selected PDF; intentionally deferred as a concept source."
      }),
      Object.freeze({
        title: "Use the Day 1 gate-logic route now",
        detail: "Follow the local Day 1 map and its owned-textbook sections for gates, Boolean algebra, combinational blocks, and HDL. The selected HLStrans paper does not replace them.",
        stage: "Preflight and Stages 1-4",
        artifact: "XOR, half-adder, mux/latch, and width/sign artifacts named by the Day 1 map.",
        gate: "MANUAL truth-table/boundary checks plus the LOCAL preflight environment check.",
        source: "Local pdf_reading_map.md; textbook required separately",
        url: "pdf_reading_map.md",
        kind: "reading",
        availability: "Local route available; textbook source must be added or opened separately."
      })
    ]),
    day2: Object.freeze([
      Object.freeze({
        title: "Keep HLStrans deferred while learning state",
        detail: "Confirm the source identity only. HLStrans pages 1-2 do not teach edge semantics, nonblocking assignment, FSMs, or ready/valid timing.",
        stage: "Day 2 source routing; deferred to Stage 11A",
        artifact: "A source note that separates HLS claims from the sequential-RTL prerequisites still being learned.",
        gate: "MANUAL: no Day 2 stage is marked complete from this paper.",
        source: "Selected HLStrans PDF, title page and abstract",
        url: null,
        kind: "reading",
        availability: "Available in the selected PDF; intentionally deferred as a concept source."
      }),
      Object.freeze({
        title: "Use the Day 2 sequential-logic route now",
        detail: "Follow the local Day 2 map, textbook state/FSM sections, waveform walkthrough, and explicit ready/valid transfer rule.",
        stage: "Stages 5-8",
        artifact: "Old/new-state predictions, FSM tables, E0-E10 trace, and ready/valid classification.",
        gate: "MANUAL stage checks plus LOCAL make sim and quiz >= 8/10 for Stage 7.",
        source: "Local pdf_reading_map.md; textbook and local trace guides",
        url: "pdf_reading_map.md",
        kind: "reading",
        availability: "Local route available; textbook source must be added or opened separately."
      })
    ]),
    day3: Object.freeze([
      Object.freeze({
        title: "Task, motivation, and transformation categories",
        detail: "Read PDF page 1. Explain why C-to-HLS is not direct textual translation and record the five named transformation categories in your own words.",
        stage: "Stage 11A, reading 1 of 2",
        artifact: "Evidence-ledger claim and extraction for task motivation and all five transformations.",
        gate: "MANUAL: wording is traceable to page 1 and separated from your interpretation.",
        source: "HLStrans arXiv:2507.04315v3, PDF page 1",
        url: null,
        kind: "reading",
        availability: "Available after title-page identity confirmation."
      }),
      Object.freeze({
        title: "Dataset entry, synthesis annotations, and evidence limits",
        detail: "Read PDF pages 1-2. Record the C/HLS/testbench triple, synthesis-derived latency/resource annotations, paired-testbench role, and evidence still missing from these pages.",
        stage: "Stage 11A, reading 2 of 2",
        artifact: "One complete HLStrans evidence-ledger row using the field-guide template.",
        gate: "MANUAL: claims are separated from inspected evidence; no CoSim or fair-QoR result is invented.",
        source: "HLStrans arXiv:2507.04315v3, PDF pages 1-2",
        url: null,
        kind: "reading",
        availability: "Available after title-page identity confirmation."
      })
    ])
  });

  const UNMAPPED_READING_FOCUS = Object.freeze({
    day1: "Search the contents/index for logic levels, number systems, Boolean algebra, gates, truth tables, muxes, adders, HDL combinational logic, width, and signedness.",
    day2: "Search the contents/index for registers, flip-flops, clock edges, nonblocking assignment, reset, finite-state machines, waveforms, latency, and ready/valid transfers.",
    day3: "Search the contents/index for testbenches, independent oracles, coverage, synthesis, QoR, C-to-HLS, CoSim, tool versions, and evidence limitations."
  });

  const RUNS = Object.freeze({
    day1: Object.freeze([
      Object.freeze({
        title: "MIT OCW: Combinational Devices (6:06)",
        detail: "Watch once, then pause the source and connect device behavior to the XOR and half-adder truth tables.",
        stage: "Stages 1-2",
        artifact: "Annotate each XOR/half-adder row with the gate-level behavior that produces it.",
        gate: "MANUAL: all four input rows and both half-adder outputs are explained without the video open.",
        source: "MIT OpenCourseWare, 6.004 Computation Structures",
        url: "https://ocw.mit.edu/courses/6-004-computation-structures-spring-2017/pages/c2/c2s2/c2s2v4/",
        kind: "video",
        availability: "Available online; no account or local HDL tool required."
      }),
      Object.freeze({
        title: "MIT OCW: Sum of Products (9:38)",
        detail: "Use the worked Boolean form to move from a truth table to a complete combinational expression before coding the mux.",
        stage: "Stages 1 and 3",
        artifact: "Derive one sum-of-products expression and show its equivalence to the mux truth table.",
        gate: "MANUAL: the expression matches every truth-table row and both mux implementations.",
        source: "MIT OpenCourseWare, 6.004 Computation Structures",
        url: "https://ocw.mit.edu/courses/6-004-computation-structures-spring-2017/pages/c4/c4s2/",
        kind: "video",
        availability: "Available online; no account or local HDL tool required."
      })
    ]),
    day2: Object.freeze([
      Object.freeze({
        title: "MIT OCW: Sequential Circuit Timing",
        detail: "Predict the state table first, watch the timing explanation, then correct the table in a different color and explain every change.",
        stage: "Stage 5",
        artifact: "Pre-edge RHS and committed post-edge values for E1-E3.",
        gate: "MANUAL: q1/q2 predictions obey old-state/new-state semantics.",
        source: "MIT OpenCourseWare official YouTube, 6.004 Computation Structures",
        url: "https://www.youtube.com/watch?v=3LQUrpSADx8",
        kind: "video",
        availability: "Available online; no local simulator required."
      }),
      Object.freeze({
        title: "MIT OCW: Finite State Machines topic videos",
        detail: "Use the videos for state/transition/output structure, then run the local E0-E10 waveform as a separate guided timing exercise.",
        stage: "Stages 6-8",
        artifact: "FSM reset/transition/output tables plus the local waveform and handshake classifications.",
        gate: "MANUAL FSM/handshake checks and LOCAL make sim plus quiz >= 8/10.",
        source: "MIT OpenCourseWare, 6.004 Computation Structures; local waveform lab",
        url: "https://ocw.mit.edu/courses/6-004-computation-structures-spring-2017/pages/c6/c6s2/",
        kind: "video",
        availability: "Available online; the local waveform run uses this repository."
      })
    ]),
    day3: Object.freeze([
      Object.freeze({
        title: "AMD: Vitis HLS Overview",
        detail: "Use this orientation to identify the C model, synthesis step, generated RTL, and report boundary; do not treat watching as tool evidence.",
        stage: "Stages 10-11",
        artifact: "A four-step flow sketch labeled with the evidence created at each step.",
        gate: "MANUAL: distinguish overview claims from LOCAL RTL checks and EXTERNAL HLS results.",
        source: "AMD official YouTube",
        url: "https://www.youtube.com/watch?v=xU9WsqcICR0",
        kind: "video",
        availability: "Available online; no tool required to watch."
      }),
      Object.freeze({
        title: "AMD 2026.1 Vitis HLS getting-started run",
        detail: "Follow the official project, simulation, synthesis, optimization, and command-line tutorial only if it matches the installed tool version; preserve commands and reports.",
        stage: "Stage 11B and Stage 12",
        artifact: "Filled HLS contract with tool/part/clock/directives plus preserved CSim, synthesis/report, and CoSim records.",
        gate: "EXTERNAL: only a real matching-version run and preserved outputs can pass.",
        source: "AMD Vitis Tutorials 2026.1",
        url: "https://docs.amd.com/r/en-US/Vitis-Tutorials-Getting-Started/Vitis-HLS",
        kind: "guided-run",
        availability: "Guide is available online; matching AMD tools are required for the EXTERNAL gate."
      })
    ])
  });

  function normalizeBlock(block) {
    return Object.hasOwn(BLOCK_LABELS, block) ? block : "day1";
  }

  function classifySource(fileName) {
    const normalized = String(fileName || "").toLowerCase();
    if (/(2507[._-]04315|hls[\s_-]*trans)/.test(normalized)) return "hlstrans";
    if (/(digital[\s_-]*design.*computer[\s_-]*architecture|\bddca\b)/.test(normalized)) return "textbook";
    return "unmapped";
  }

  function createEmptyProfile() {
    return {
      revision: 1,
      sourceOverride: "",
      identity: {
        titleAndAuthor: "",
        version: "",
        pageConvention: "",
        confirmed: false
      },
      blocks: {
        day1: { locators: [{ value: "", viewerPage: 0, confirmed: false }, { value: "", viewerPage: 0, confirmed: false }], analysis: null },
        day2: { locators: [{ value: "", viewerPage: 0, confirmed: false }, { value: "", viewerPage: 0, confirmed: false }], analysis: null },
        day3: { locators: [{ value: "", viewerPage: 0, confirmed: false }, { value: "", viewerPage: 0, confirmed: false }], analysis: null }
      }
    };
  }

  function cleanText(value) {
    return String(value || "").trim();
  }

  function normalizeProfile(profile, requestedBlock) {
    const block = normalizeBlock(requestedBlock);
    const empty = createEmptyProfile();
    const candidate = profile && typeof profile === "object" ? profile : {};
    const sourceOverride = ["textbook", "hlstrans", "unmapped"].includes(candidate.sourceOverride)
      ? candidate.sourceOverride
      : "";
    const identity = candidate.identity && typeof candidate.identity === "object" ? candidate.identity : {};
    const candidateLocators = candidate.blocks?.[block]?.locators;
    const locators = [0, 1].map((index) => ({
      value: cleanText(Array.isArray(candidateLocators) ? candidateLocators[index]?.value : ""),
      viewerPage: Number.isSafeInteger(Number(Array.isArray(candidateLocators) ? candidateLocators[index]?.viewerPage : 0))
        ? Math.max(0, Number(Array.isArray(candidateLocators) ? candidateLocators[index]?.viewerPage : 0))
        : 0,
      confirmed: Boolean(Array.isArray(candidateLocators) && candidateLocators[index]?.confirmed)
    }));
    return {
      revision: Number.isInteger(candidate.revision) && candidate.revision > 0 ? candidate.revision : empty.revision,
      sourceOverride,
      identity: {
        titleAndAuthor: cleanText(identity.titleAndAuthor),
        version: cleanText(identity.version),
        pageConvention: cleanText(identity.pageConvention),
        confirmed: Boolean(identity.confirmed)
      },
      locators,
      pdfId: cleanText(candidate.pdfId)
    };
  }

  function identityMatches(sourceType, identity) {
    const identityText = `${identity.titleAndAuthor} ${identity.version}`;
    if (!identity.confirmed || !identity.titleAndAuthor || !identity.version || !identity.pageConvention) return false;
    if (sourceType === "hlstrans") {
      return /hls\s*trans/i.test(identityText) && /(2507[._-]04315\s*v?3|arxiv:\s*2507[._-]04315v3)/i.test(identityText);
    }
    if (sourceType === "textbook") return /digital\s+design.*computer\s+architecture/i.test(identityText);
    return true;
  }

  function getSyncState(fileName, requestedBlock, profile) {
    const block = normalizeBlock(requestedBlock);
    const normalized = normalizeProfile(profile, block);
    const suggestedType = classifySource(fileName);
    const sourceType = normalized.sourceOverride || suggestedType;
    const identityConfirmed = Boolean(normalized.sourceOverride) && identityMatches(sourceType, normalized.identity);
    const locatorsConfirmed = normalized.locators.every((locator) => locator.confirmed && locator.value && locator.viewerPage > 0);
    const ready = identityConfirmed && locatorsConfirmed;
    let message;
    if (!normalized.sourceOverride) {
      message = `Filename suggestion: ${suggestedType}. Select the actual source type and confirm the title/version; the filename cannot confirm identity.`;
    } else if (!identityConfirmed) {
      message = sourceType === "hlstrans"
        ? "Sync pending: confirm the HLStrans title and arXiv:2507.04315v3, plus the PDF page-number convention. Exact HLStrans pages stay hidden until then."
        : "Sync pending: confirm the title/author, edition or version, and how PDF viewer pages map to printed pages or sections.";
    } else if (!locatorsConfirmed) {
      message = `Identity is learner-confirmed at revision ${normalized.revision}. Confirm both ${block} reading locators before generating the ready worksheet.`;
    } else {
      message = `Learner-confirmed synchronization to ${String(fileName || "this PDF")} at revision ${normalized.revision}.`;
    }
    return {
      block,
      sourceType,
      suggestedType,
      identityConfirmed,
      locatorsConfirmed,
      ready,
      status: ready ? "ready" : identityConfirmed ? "locators-pending" : "identity-pending",
      message,
      revision: normalized.revision,
      identity: normalized.identity,
      locators: normalized.locators,
      pdfId: normalized.pdfId
    };
  }

  function getUnmappedReadings(block) {
    return [
      {
        title: "Confirm this PDF's identity and navigation",
        detail: "Manually record the title, authors or organization, version/date, page count, and table-of-contents headings. The coach has not parsed this browser-only file.",
        stage: `${BLOCK_LABELS[block]} source triage`,
        artifact: "A provisional source-identity row with page-number convention and relevant-looking section titles.",
        gate: "MANUAL: values are copied from the opened PDF, not inferred from its filename.",
        source: "Selected PDF; filename-only routing until manually inspected or attached to Codex",
        url: null,
        kind: "reading",
        availability: "Available for manual inspection in the shelf; not parsed by the coach."
      },
      {
        title: "Build a stage-specific reading shortlist",
        detail: `${UNMAPPED_READING_FOCUS[block]} Record candidate pages, then attach the PDF to Codex for verified source-specific analysis if desired.`,
        stage: BLOCK_LABELS[block],
        artifact: "A two-to-five section shortlist with exact PDF pages, concepts to recall, matching exercise artifact, and evidence gate.",
        gate: "MANUAL until the shortlisted pages are inspected; no stage pass is inferred from search hits.",
        source: "Selected PDF manual search plus the local three-day reading map",
        url: "pdf_reading_map.md",
        kind: "reading",
        availability: "Manual search is available; exact guidance requires attaching the PDF in the Codex conversation."
      }
    ];
  }

  function getPendingHlsReadings(block) {
    if (block !== "day3") return Array.from(HLSTRANS_READINGS[block]);
    return [
      {
        title: "Confirm the selected HLStrans source",
        detail: "Open the title page and record the exact title, authors, arXiv identifier/version, date, and page-number convention. Exact reading pages are intentionally withheld until this identity is confirmed.",
        stage: "Stage 11A source synchronization",
        artifact: "A learner-confirmed identity record for the selected PDF.",
        gate: "MANUAL: the saved identity is copied from the opened PDF, not inferred from its filename.",
        source: "Selected PDF; filename suggests HLStrans but does not confirm it",
        url: null,
        kind: "reading",
        availability: "Available for manual title-page inspection; exact-page assignment is pending."
      },
      {
        title: "Calibrate the two HLStrans reading locators",
        detail: "After confirming arXiv:2507.04315v3, save the two viewer-page/section locators shown by the PDF and confirm both locator checkboxes.",
        stage: "Stage 11A locator synchronization",
        artifact: "Two confirmed locators tied to this PDF record and Day 3.",
        gate: "MANUAL: each locator opens the intended task/motivation or dataset/evidence material in this selected PDF.",
        source: "Selected PDF plus learner-entered locator calibration",
        url: null,
        kind: "reading",
        availability: "Available after learner-confirmed source identity."
      }
    ];
  }

  function getReadings(sourceType, block, identityConfirmed) {
    if (sourceType === "textbook") return Array.from(TEXTBOOK_READINGS[block]);
    if (sourceType === "hlstrans") return identityConfirmed ? Array.from(HLSTRANS_READINGS[block]) : getPendingHlsReadings(block);
    return getUnmappedReadings(block);
  }

  function getReadingAssignment(fileName, requestedBlock, profile = {}) {
    const block = normalizeBlock(requestedBlock);
    const blockLabel = BLOCK_LABELS[block];
    const sync = getSyncState(fileName, block, profile);
    const sourceType = sync.sourceType;
    const guide = TEXTBOOK_GUIDES[block];
    const readings = getReadings(sourceType, block, sync.identityConfirmed).map((reading, index) => ({
      ...reading,
      locator: sync.locators[index].value || "Not calibrated for this PDF",
      viewerPage: sync.locators[index].viewerPage,
      locatorConfirmed: sync.locators[index].confirmed && Boolean(sync.locators[index].value) && Number.isSafeInteger(sync.locators[index].viewerPage) && sync.locators[index].viewerPage > 0
    }));
    const runs = Array.from(RUNS[block]);
    const syncPrefix = sync.ready ? "PDF sync ready" : "PDF sync pending";

    if (sourceType === "hlstrans") {
      const hlsSteps = block === "day3" && !sync.identityConfirmed
        ? [
          "Confirm the exact HLStrans title, authors, arXiv version, date, and page-number convention from the opened PDF.",
          "Save and confirm both Day 3 section labels and viewer-page locators.",
          "Only then use the revealed exact-page assignment to create the evidence-ledger row."
        ]
        : Array.from(guide.steps);
      return {
        sourceType,
        badge: `${syncPrefix} / HLStrans`,
        title: block === "day3" && sync.identityConfirmed ? "Read HLStrans in two synchronized passes" : block === "day3" ? "Confirm HLStrans before exact-page reading" : `HLStrans is deferred; follow the ${block === "day1" ? "Day 1" : "Day 2"} route`,
        summary: `${sync.message} The two reading cards state whether this source is relevant now or deferred.`,
        steps: hlsSteps,
        artifact: block === "day3" ? "Produce one HLStrans evidence-ledger row using the field-guide template." : guide.artifact,
        gate: block === "day3" ? "MANUAL Stage 11A only. Reading cannot satisfy the EXTERNAL Stage 11B HLS-tool gate." : guide.gate,
        blockLabel,
        readings,
        runs,
        sync
      };
    }

    if (sourceType === "textbook") {
      return {
        sourceType,
        badge: `${syncPrefix} / Textbook`,
        title: guide.title,
        summary: `${sync.message} Match section titles before trusting viewer page numbers. Use the two reading cards in order.`,
        steps: Array.from(guide.steps),
        artifact: guide.artifact,
        gate: guide.gate,
        blockLabel,
        readings,
        runs,
        sync
      };
    }

    return {
      sourceType,
      badge: `${syncPrefix} / Other source`,
      title: "Triage this PDF in two passes before reading it linearly",
      summary: `${sync.message} Optional local analysis can suggest exactly two text-matched pages after explicit consent; suggestions remain unconfirmed. The two reading cards are followed by two trusted block-level runs.`,
      steps: [
        "Complete both reading cards without assuming the filename describes the contents.",
        "Use both video/guided-run cards and produce their named artifacts.",
        "Preview and confirm local candidates, or attach the PDF separately in Codex when you want a deeper source review."
      ],
      artifact: "Produce one provisional evidence-ledger row and a stage-specific reading assignment.",
      gate: "MANUAL until the source is attached and its relevant pages are inspected; never infer a pass from the filename alone.",
      blockLabel,
      readings,
      runs,
      sync
    };
  }

  function buildCodexPrompt(fileName, requestedBlock, profile = {}) {
    const assignment = getReadingAssignment(fileName, requestedBlock, profile);
    return [
      `I will attach the PDF named "${String(fileName || "unnamed.pdf")}" to this conversation.`,
      `My current RTL sprint block is: ${assignment.blockLabel}.`,
      "Analyze the attached PDF for this study block and return:",
      "1. Verified source identity, version/date, and page count.",
      "2. Exactly two prioritized reading assignments with PDF pages and section titles, in dependency order.",
      "3. Concepts I must explain from memory after each assignment.",
      "4. The artifact I must produce for each matching exercise stage.",
      "5. The MANUAL, LOCAL, or EXTERNAL gate that checks each artifact.",
      "6. Two relevant official videos or guided runs, with honest availability and no fabricated links.",
      "7. Author claims versus evidence actually inspected, plus missing evidence.",
      "8. Sections I should skip or defer during this three-day sprint.",
      "Do not claim access to the browser's private PDF shelf; use only the PDF attached in this conversation."
    ].join("\n");
  }

  function buildOutputWorksheet(fileName, requestedBlock, profile = {}) {
    const assignment = getReadingAssignment(fileName, requestedBlock, profile);
    const sync = assignment.sync;
    const lines = [
      "# RTL study output worksheet",
      "",
      `Status: ${sync.ready ? "SYNC READY" : "SYNC PENDING - confirm identity and both reading locators before treating this as source-synchronized"}`,
      `Selected PDF: ${String(fileName || "unnamed.pdf")}`,
      `PDF shelf record: ${sync.pdfId || "not recorded"}`,
      `Study block: ${assignment.blockLabel}`,
      `Source type: ${assignment.sourceType}`,
      `Identity revision: ${sync.revision}`,
      `Confirmed title/author: ${sync.identity.titleAndAuthor || "[fill in]"}`,
      `Edition/version/date/identifier: ${sync.identity.version || "[fill in]"}`,
      `Page-number convention: ${sync.identity.pageConvention || "[fill in]"}`,
      `Synchronization note: ${sync.message}`,
      ""
    ];
    assignment.readings.forEach((reading, index) => {
      lines.push(
        `## Reading ${index + 1}: ${reading.title}`,
        `Stage: ${reading.stage}`,
        `Confirmed locator: ${reading.locatorConfirmed ? `PDF viewer page ${reading.viewerPage} - ${reading.locator}` : "[SYNC PENDING: enter viewer page and section, then confirm in the shelf]"}`,
        `Source/provenance: ${reading.source}`,
        `Read for: ${reading.detail}`,
        "Explain from memory:",
        "- [write your explanation]",
        "Evidence inspected:",
        "- [pages, sections, tables, figures, or examples actually inspected]",
        `Required artifact: ${reading.artifact}`,
        `Gate: ${reading.gate}`,
        "Gate result: [PASS / BLOCKED, with evidence path or reason]",
        ""
      );
    });
    assignment.runs.forEach((run, index) => {
      lines.push(
        `## Run ${index + 1}: ${run.title}`,
        `Kind/stage: ${run.kind} / ${run.stage}`,
        `Trusted source: ${run.source}`,
        `Link: ${run.url || "No link configured"}`,
        `Availability: ${run.availability}`,
        "Prediction before watching/running:",
        "- [write what you expect]",
        "Steps or commands performed:",
        "- [record exact steps]",
        "Observations:",
        "- [record what changed or was confirmed]",
        `Required artifact: ${run.artifact}`,
        `Gate: ${run.gate}`,
        "Gate result: [PASS / BLOCKED, with evidence path or reason]",
        ""
      );
    });
    lines.push(
      "## Claims versus evidence",
      "- Claim:",
      "- Evidence actually inspected:",
      "- Missing or unresolved evidence:",
      "",
      "## Overall decision",
      "- [PASS current block / BLOCKED]",
      "- Reason and next action:"
    );
    return lines.join("\n");
  }

  const api = Object.freeze({
    BLOCK_LABELS,
    buildCodexPrompt,
    buildOutputWorksheet,
    classifySource,
    createEmptyProfile,
    getSyncState,
    getReadingAssignment
  });

  globalObject.RTL_READING_COACH = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(globalThis);
