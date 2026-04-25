import json
import logging
import os
import re
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import requests


# =========================================================
# CONFIGURATION
# =========================================================
@dataclass(frozen=True)
class PipelineConfig:
    """
    Configuration for the DANS Dataverse qualitative-project pipeline.
    """
    base_url: str = "https://ssh.datastations.nl/api"
    search_url: str = "https://ssh.datastations.nl/api/search"
    dataset_url: str = "https://ssh.datastations.nl/api/datasets/:persistentId"

    query: Optional[List[str]] = None
    per_page: int = 1000
    timeout: int = 60

    base_dir: str = "downloads"
    repo_name: str = "dans"
    db_path: str = "23048230-seeding.db"

    repository_id: int = 5
    repository_url: str = "https://ssh.datastations.nl"
    download_method: str = "API-CALL"

    summary_filename: str = "dans_repo5_summary.json"
    temp_dir_name: str = "_tmp"

    log_level: int = logging.INFO
    log_format: str = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    log_date_format: str = "%Y-%m-%d %H:%M:%S"

    @property
    def script_dir(self) -> str:
        return os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()

    @property
    def schema_path(self) -> str:
        return os.path.join(self.script_dir, "schemas", "schema.sql")

    @property
    def repo_dir(self) -> str:
        return os.path.join(self.base_dir, self.repo_name)

    @property
    def repo_temp_dir(self) -> str:
        return os.path.join(self.repo_dir, self.temp_dir_name)

    @property
    def summary_path(self) -> str:
        return os.path.join(self.script_dir, self.summary_filename)


# =========================================================
# MASTER KEYWORD REGISTRY
# =========================================================

# ---------------------------------------------------------
# SECTION A — Core QDA / Qualitative Method Terms (English)
# ---------------------------------------------------------
_CORE_QDA_EN: List[str] = [
    "qualitative",
    "qualitative data",
    "qualitative research",
    "qualitative analysis",
    "qualitative study",
    "qualitative inquiry",
    "qualitative methods",
    "qualitative methodology",
    "QDA",
    "qualitative data analysis",
    "CAQDAS",
    "computer-assisted qualitative data analysis",
    "computer-assisted qualitative",
    "qualitative data analysis file",
    "QDA project",
    "QDA Codebook",
    "QDA export",
    "REFI-QDA",
    "REFI project",
    "Qualitative Data Exchange",
    "QDE format",
    "interoperable QDA",
    "QDA project exchange",
]

# ---------------------------------------------------------
# SECTION B — Interview-Based Methods (English)
# ---------------------------------------------------------
_INTERVIEW_EN: List[str] = [
    "interview",
    "interviews",
    "in-depth interview",
    "semi-structured interview",
    "unstructured interview",
    "structured interview",
    "life history interview",
    "biographical interview",
    "narrative interview",
    "oral history",
    "oral histories",
    "interview transcript",
    "interview transcripts",
    "interview data",
    "interview recording",
    "respondent",
    "verbatim transcript",
    "verbatim",
    "plain text transcript",
    "interview text file",
    "interview audio file",
    "oral history audio",
    "interview protocol",
    "topic guide",
    "interview guide",
]

# ---------------------------------------------------------
# SECTION C — Group & Participatory Methods (English)
# ---------------------------------------------------------
_GROUP_EN: List[str] = [
    "focus group",
    "focus groups",
    "group discussion",
    "group interview",
    "participatory research",
    "participatory action research",
    "PAR",
    "co-production",
    "community-based research",
]

# ---------------------------------------------------------
# SECTION D — Ethnographic & Observational Methods (English)
# ---------------------------------------------------------
_ETHNOGRAPHY_EN: List[str] = [
    "ethnography",
    "ethnographic",
    "ethnographic fieldwork",
    "fieldwork",
    "field notes",
    "fieldnotes",
    "observation",
    "participant observation",
    "non-participant observation",
    "observational data",
    "video observation",
    "diary",
    "diaries",
    "research diary",
    "fieldwork diary",
    "logbook",
    "visual methods data",
    "video ethnography",
]

# ---------------------------------------------------------
# SECTION E — Text, Narrative & Discourse (English)
# ---------------------------------------------------------
_NARRATIVE_EN: List[str] = [
    "narrative",
    "narratives",
    "narrative analysis",
    "discourse analysis",
    "discourse",
    "conversation analysis",
    "content analysis",
    "textual analysis",
    "document analysis",
    "written accounts",
    "personal accounts",
    "life story",
    "life stories",
    "autobiography",
    "autobiographical",
    "biography",
    "biographical",
    "critical discourse analysis",
    "CDA",
]

# ---------------------------------------------------------
# SECTION F — Analytical Frameworks (English)
# ---------------------------------------------------------
_FRAMEWORKS_EN: List[str] = [
    "grounded theory",
    "thematic analysis",
    "thematic coding",
    "phenomenology",
    "phenomenological",
    "interpretive phenomenological analysis",
    "IPA",
    "framework analysis",
    "constant comparative method",
    "hermeneutics",
    "hermeneutical",
    "interpretive",
    "constructivist",
    "constructivism",
    "phenomenography",
]

# ---------------------------------------------------------
# SECTION G — Data Types & File Content (English)
# ---------------------------------------------------------
_DATA_TYPES_EN: List[str] = [
    "transcript",
    "transcripts",
    "transcription",
    "audio recording",
    "audio data",
    "video recording",
    "video data",
    "image data",
    "photographs",
    "visual data",
    "text data",
    "written data",
    "open-ended responses",
    "open-ended questions",
    "free text",
    "codebook",
    "coding scheme",
    "codes",
    "memos",
    "analytic memos",
    "coded sources",
    "coded segments",
    "coding tree",
    "node hierarchy",
    "code system",
    "code frequency",
    "case node",
    "source file",
    "external source",
    "internal source",
    "primary document",
    "hermeneutic unit",
    "network view",
    "memo link",
]

# ---------------------------------------------------------
# SECTION H — QDA Software (English)
# ---------------------------------------------------------
_SOFTWARE_EN: List[str] = [
    "NVivo",
    "NVivo project file",
    "ATLAS.ti",
    "ATLAS.ti copy bundle",
    "MAXQDA",
    "MAXQDA project",
    "Dedoose",
    "Quirkos",
    "Transana",
    "HyperRESEARCH",
    "JASP",
]

# ---------------------------------------------------------
# SECTION I — Social Science Disciplines (English)
# ---------------------------------------------------------
_DISCIPLINES_EN: List[str] = [
    "sociology",
    "sociological",
    "anthropology",
    "anthropological",
    "social work",
    "social policy",
    "criminology",
    "political science",
    "education research",
    "health research",
    "public health",
    "psychology",
    "social psychology",
    "human geography",
    "gender studies",
    "migration",
    "refugee",
    "identity",
    "diversity",
    "inclusion",
]

# ---------------------------------------------------------
# SECTION J — Applied & Subject-Specific Terms (English)
# ---------------------------------------------------------
_APPLIED_EN: List[str] = [
    "patient experience",
    "lived experience",
    "wellbeing",
    "mental health",
    "illness narrative",
    "healthcare",
    "poverty",
    "inequality",
    "labour",
    "work experience",
    "family",
    "childhood",
    "youth",
    "ageing",
    "religion",
    "community",
]

# ---------------------------------------------------------
# SECTION K — Methodological & Study Design Terms (English)
# ---------------------------------------------------------
_STUDY_DESIGN_EN: List[str] = [
    "mixed methods",
    "case study",
    "case studies",
    "longitudinal qualitative",
    "repeated interview",
    "panel study",
    "secondary analysis",
    "archival research",
    "retrospective",
    "prospective",
    "sampling",
    "purposive sampling",
    "snowball sampling",
    "theoretical sampling",
    "saturation",
    "theoretical saturation",
    "triangulation",
    "exploratory research",
    "interpretive research",
    "action research",
    "evaluation research qualitative",
    "policy research qualitative",
    "emancipatory research",
    "critical research",
    "reflexive research",
]

# ---------------------------------------------------------
# SECTION L — Repository & Archival Terms (English)
# ---------------------------------------------------------
_REPOSITORY_EN: List[str] = [
    "deposited data",
    "archived data",
    "raw data",
    "primary data",
    "secondary data",
    "data deposit",
    "data sharing",
    "reusable data",
    "DANS",
    "EASY",
    "DataverseNL",
    "NARCIS",
    "DANS Data Vault",
    "DANS EASY",
    "EASY deposit",
    "DANS catalogue",
    "data catalogue",
    "persistent identifier",
    "DOI dataset",
    "URN:NBN",
    "open access dataset",
    "restricted access dataset",
    "embargoed dataset",
    "data reuse",
    "secondary data reuse",
    "deposited dataset",
    "research data deposit",
    "FAIR data",
    "findable accessible interoperable reusable",
    "metadata record",
    "dataset metadata",
    "Dublin Core metadata",
    "DDI metadata",
    "DDI Lifecycle",
    "DDI Codebook",
    "data documentation initiative",
]

# ---------------------------------------------------------
# SECTION M — DANS Metadata Field Keywords
# ---------------------------------------------------------
_DANS_METADATA_FIELDS: List[str] = [
    "audience: researchers",
    "subject: qualitative methods",
    "subject: sociology",
    "subject: anthropology",
    "subject: interview research",
    "language: Dutch",
    "language: Nederlands",
    "data collection method: interview",
    "data collection method: observation",
    "data collection method: focus group",
    "time period covered",
    "geographic coverage: Netherlands",
    "geographic coverage: Nederland",
    "kind of data: qualitative",
    "kind of data: text",
    "kind of data: audio",
    "kind of data: video",
    "unit of analysis: individual",
    "unit of analysis: group",
    "universe: adults",
    "sampling procedure: purposive",
    "sampling procedure: snowball",
    "Data Station Social Sciences",
    "Data Station SSH",
    "SSH data station",
    "Social Sciences and Humanities",
]

# ---------------------------------------------------------
# SECTION N — Access & Licence Keywords
# ---------------------------------------------------------
_ACCESS_LICENCE: List[str] = [
    "open access",
    "Creative Commons",
    "CC BY",
    "CC BY-SA",
    "CC0",
    "public domain",
    "freely downloadable",
    "no restrictions",
    "anonymous data",
    "anonymised data",
    "anonymised transcripts",
    "informed consent",
    "restricted use",
    "access request",
]

# ---------------------------------------------------------
# SECTION O — Core QDA / Qualitative Method Terms (Dutch)
# ---------------------------------------------------------
_CORE_QDA_NL: List[str] = [
    "kwalitatief",
    "kwalitatieve data",
    "kwalitatief onderzoek",
    "kwalitatieve analyse",
    "kwalitatieve studie",
    "kwalitatieve methoden",
    "kwalitatieve methodologie",
    "kwalitatieve onderzoeksmethoden",
    "kwalitatieve benadering",
    "kwalitatieve gegevens",
    "kwalitatieve analysesoftware",
    "computerondersteunde kwalitatieve analyse",
    "coderingssoftware",
    "analyseproject",
]

# ---------------------------------------------------------
# SECTION P — Interview-Based Methods (Dutch)
# ---------------------------------------------------------
_INTERVIEW_NL: List[str] = [
    "diepte-interview",
    "diepte-interviews",
    "semi-gestructureerd interview",
    "ongestructureerd interview",
    "gestructureerd interview",
    "levensgeschiedenisinterview",
    "biografisch interview",
    "narratief interview",
    "mondeling geschiedenis",
    "mondelinge geschiedenis",
    "mondelinge geschiedenis onderzoek",
    "interviewtranscript",
    "interviewtranscripten",
    "interviewdata",
    "geluidsopname interview",
    "geïnterviewde",
    "informant",
    "gesprekspartner",
    "verbatim verslag",
    "woordelijk transcript",
    "gespreksverslag",
    "uitwerking interview",
    "uitgeschreven interview",
    "interviewuitwerking",
    "interviewprotocol",
    "topiclijst",
    "vragenlijst kwalitatief",
    "interviewleidraad",
    "gesprekshandleiding",
    "getranscribeerde interviews",
    "interviewopname",
    "bandopname",
    "geluidsband",
    "videoband",
    "cassetteband",
    "digitale opname",
    "gespreksopname",
    "opnameapparatuur",
]

# ---------------------------------------------------------
# SECTION Q — Group & Participatory Methods (Dutch)
# ---------------------------------------------------------
_GROUP_NL: List[str] = [
    "focusgroep",
    "focusgroepen",
    "groepsdiscussie",
    "groepsinterview",
    "participatief onderzoek",
    "participatieve actieonderzoek",
    "co-productie onderzoek",
    "gemeenschapsonderzoek",
    "burgeronderzoek",
    "bewonersonderzoek",
]

# ---------------------------------------------------------
# SECTION R — Ethnographic & Observational Methods (Dutch)
# ---------------------------------------------------------
_ETHNOGRAPHY_NL: List[str] = [
    "etnografie",
    "etnografisch",
    "etnografisch veldwerk",
    "veldwerk",
    "veldnotities",
    "veldaantekeningen",
    "observatie",
    "participerende observatie",
    "niet-participerende observatie",
    "observatiedata",
    "video-observatie",
    "dagboek",
    "dagboeken",
    "onderzoeksdagboek",
    "veldwerkdagboek",
]

# ---------------------------------------------------------
# SECTION S — Text, Narrative & Discourse (Dutch)
# ---------------------------------------------------------
_NARRATIVE_NL: List[str] = [
    "narratieven",
    "narratieve analyse",
    "discoursanalyse",
    "discours",
    "conversatieanalyse",
    "gespreksanalyse",
    "inhoudsanalyse",
    "tekstanalyse",
    "documentanalyse",
    "persoonlijke verhalen",
    "levensverhaal",
    "levensverhalen",
    "levensloop",
    "autobiografie",
    "autobiografisch",
    "biografie",
    "biografisch",
    "geschreven verslagen",
    "persoonlijke verslagen",
    "verhaal",
    "verhalen",
    "ervaringsverhalen",
    "persoonlijk verhaal",
    "mondelinge overlevering",
    "herinneringen",
    "herinnering onderzoek",
    "geheugenonderzoek",
]

# ---------------------------------------------------------
# SECTION T — Analytical Frameworks (Dutch)
# ---------------------------------------------------------
_FRAMEWORKS_NL: List[str] = [
    "gefundeerde theorie",
    "thematische analyse",
    "thematische codering",
    "fenomenologie",
    "fenomenologisch",
    "interpretatieve fenomenologische analyse",
    "framework analyse",
    "constante vergelijkende methode",
    "hermeneutiek",
    "hermeneutisch",
    "interpretatief",
    "constructivisme",
    "constructivistisch",
    "kritische discoursanalyse",
    "fenomenografie",
    "fenomenografisch",
]

# ---------------------------------------------------------
# SECTION U — Data Types & File Formats (Dutch)
# ---------------------------------------------------------
_DATA_TYPES_NL: List[str] = [
    "transcriptie",
    "transcripties",
    "geluidsopname",
    "audiodata",
    "geluidsdata",
    "video-opname",
    "videodata",
    "beeldmateriaal",
    "visuele data",
    "tekstdata",
    "open vragen",
    "open antwoorden",
    "vrije tekst",
    "codeboek",
    "codeerschema",
    "analytische memo's",
    "onderzoeksmemo's",
]

# ---------------------------------------------------------
# SECTION V — Social Science Disciplines (Dutch)
# ---------------------------------------------------------
_DISCIPLINES_NL: List[str] = [
    "sociologie",
    "sociologisch onderzoek",
    "antropologie",
    "antropologisch",
    "sociale wetenschappen",
    "maatschappelijk werk",
    "sociaal beleid",
    "criminologie",
    "politicologie",
    "onderwijsonderzoek",
    "gezondheidsonderzoek",
    "volksgezondheid",
    "sociale psychologie",
    "sociale geografie",
    "genderstudies",
    "vluchteling",
]

# ---------------------------------------------------------
# SECTION W — Applied & Subject-Specific Terms (Dutch)
# ---------------------------------------------------------
_APPLIED_NL: List[str] = [
    "patiëntervaring",
    "patiënten perspectief",
    "leefervaring",
    "geleefde ervaring",
    "welzijn",
    "geestelijke gezondheid",
    "ziekte-narratief",
    "gezondheidszorg",
    "zorgervaring",
    "armoede",
    "ongelijkheid",
    "arbeid",
    "werkervaring",
    "werkomstandigheden",
    "gezinsleven",
    "kindertijd",
    "jeugd",
    "ouderen",
    "vergrijzing",
    "religie",
    "gemeenschap",
    "buurt",
    "wijk",
    "burgerperspectief",
    "sociale interactie",
    "alledaags leven",
    "dagelijks leven onderzoek",
    "betekenisgeving",
    "zingeving",
    "beleving",
    "belevingsonderzoek",
    "mentaliteit",
    "cultuuronderzoek",
    "tradities",
]

# ---------------------------------------------------------
# SECTION X — Methodological & Study Design Terms (Dutch)
# ---------------------------------------------------------
_STUDY_DESIGN_NL: List[str] = [
    "gemengde methoden",
    "mixed methods onderzoek",
    "casestudie",
    "casestudies",
    "enkelvoudige casestudie",
    "meervoudige casestudie",
    "longitudinaal kwalitatief onderzoek",
    "herhaald interview",
    "panelstudie",
    "secundaire analyse",
    "archiefonderzoek",
    "retrospectief onderzoek",
    "steekproef",
    "doelgerichte steekproef",
    "sneeuwbalsteekproef",
    "theoretische steekproef",
    "theoretische saturatie",
    "methodentriangulatie",
    "exploratief onderzoek",
    "interpretatief onderzoek",
    "actieonderzoek",
    "evaluatieonderzoek kwalitatief",
    "beleidsonderzoek kwalitatief",
    "praktijkonderzoek",
    "ontwerponderzoek",
    "participatief evaluatieonderzoek",
    "responsief evaluatieonderzoek",
    "emancipatoir onderzoek",
    "kritisch onderzoek",
    "reflexief onderzoek",
]

# ---------------------------------------------------------
# SECTION Y — Repository & Archival Terms (Dutch)
# ---------------------------------------------------------
_REPOSITORY_NL: List[str] = [
    "gearchiveerde data",
    "gedeponeerde data",
    "ruwe data",
    "primaire data",
    "secundaire data",
    "datadepot",
    "data delen",
    "herbruikbare data",
    "dataarchief",
    "DANS archief",
    "EASY archief",
    "onderzoeksdata",
    "onderzoeksdataset",
    "databeheer",
    "datamanagement",
    "onderzoeksarchief",
    "wetenschappelijk archief",
    "geanonimiseerde data",
    "geanonimiseerde transcripten",
    "toestemming deelnemers",
    "privacy gecleaned",
    "beperkte toegang",
    "toegangsverzoek",
    "vrij toegankelijk",
]

# ---------------------------------------------------------
# SECTION Z — Dutch Institution Keywords
# ---------------------------------------------------------
_INSTITUTIONS_NL: List[str] = [
    "Universiteit van Amsterdam",
    "Vrije Universiteit Amsterdam",
    "Universiteit Utrecht",
    "Radboud Universiteit",
    "Erasmus Universiteit Rotterdam",
    "Tilburg University",
    "Rijksuniversiteit Groningen",
    "Universiteit Maastricht",
    "Leiden Universiteit",
    "Universiteit Twente",
    "TU Delft",
    "Wageningen University",
    "Open Universiteit",
    "Hogeschool",
    "HBO onderzoek",
    "lectoraatsonderzoek",
    "kenniscentrum",
    "promotieonderzoek",
    "proefschrift kwalitatief",
    "dissertatie kwalitatief",
    "promotieonderzoek kwalitatief",
]

# ---------------------------------------------------------
# SECTION AA — Dutch Funder & Programme Keywords
# ---------------------------------------------------------
_FUNDERS_NL: List[str] = [
    "NWO",
    "NWO onderzoek",
    "NWO Vidi",
    "NWO Veni",
    "NWO Vici",
    "NWO Open Competition",
    "NWO MaGW",
    "NWO SGW",
    "ZonMw",
    "ZonMw onderzoek",
    "ZonMw programma",
    "ZonMw Geestkracht",
    "ZonMw Palliantie",
    "ZonMw Gender en Gezondheid",
    "KNAW",
    "KNAW project",
    "Sociaal Cultureel Planbureau",
    "SCP onderzoek",
    "CBS kwalitatief",
    "WODC onderzoek",
    "Planbureau onderzoek",
    "Horizon 2020 qualitative",
    "Horizon Europe qualitative",
    "ERC qualitative",
    "EU project kwalitatief",
]

# ---------------------------------------------------------
# SECTION AH–AS — Compound Search Pairs
# All compounds are formatted as plain-text Dataverse API strings
# (quoted phrases with space separation)
# ---------------------------------------------------------
_COMPOUNDS: List[str] = [
    # Interview compounds (Dutch)
    "diepte-interview transcriptie",
    "diepte-interview transcript",
    "diepte-interview dataset",
    "diepte-interview geluidsopname",
    "diepte-interview audiodata",
    "diepte-interview NVivo",
    "diepte-interview ATLAS.ti",
    "diepte-interview MAXQDA",
    "diepte-interview codeboek",
    "diepte-interview kwalitatieve data",
    "semi-gestructureerd interview transcriptie",
    "semi-gestructureerd interview dataset",
    "semi-gestructureerd interview codeboek",
    "semi-gestructureerd interview NVivo",
    "semi-gestructureerd interview audiodata",
    "biografisch interview transcriptie",
    "biografisch interview dataset",
    "narratief interview transcript",
    "narratief interview dataset",
    "levensgeschiedenisinterview transcriptie",
    # Interview compounds (English)
    "in-depth interview transcript",
    "in-depth interview dataset",
    "in-depth interview NVivo",
    "in-depth interview codebook",
    "in-depth interview audio recording",
    "interview transcript qualitative data",
    "interview transcript dataset",
    "interview transcript DANS",
    "interview data NVivo",
    "interview data ATLAS.ti",
    # Focus group compounds (Dutch)
    "focusgroep dataset",
    "focusgroep transcriptie",
    "focusgroep transcript",
    "focusgroep geluidsopname",
    "focusgroep kwalitatieve data",
    "focusgroep NVivo",
    "focusgroep codeboek",
    "focusgroep MAXQDA",
    "focusgroepen dataset",
    "focusgroepen transcriptie",
    # Focus group compounds (English)
    "focus group transcript",
    "focus group dataset",
    "focus group NVivo",
    "focus group audio recording",
    "focus group qualitative data",
    "groepsdiscussie transcriptie",
    "groepsdiscussie dataset",
    "groepsinterview transcript",
    "groepsinterview dataset",
    "group discussion transcript",
    # Ethnography compounds (Dutch)
    "etnografie veldnotities",
    "etnografie dataset",
    "etnografie observatiedata",
    "etnografie veldaantekeningen",
    "etnografisch veldwerk dataset",
    "etnografisch veldwerk transcriptie",
    "participerende observatie veldnotities",
    "participerende observatie dataset",
    "participerende observatie dagboek",
    "veldwerk dataset",
    "veldwerk transcriptie",
    "veldwerk NVivo",
    # Ethnography compounds (English)
    "ethnography field notes",
    "ethnography dataset",
    "ethnography NVivo",
    "participant observation field notes",
    "participant observation dataset",
    "fieldwork transcript",
    "fieldwork dataset",
    "field notes qualitative data",
    # Narrative & Discourse compounds (Dutch)
    "levensverhaal dataset",
    "levensverhaal transcriptie",
    "levensverhaal interview",
    "levensverhalen dataset",
    "levensverhalen transcriptie",
    "narratieve analyse dataset",
    "discoursanalyse dataset",
    "discoursanalyse tekstdata",
    "inhoudsanalyse dataset",
    "inhoudsanalyse transcriptie",
    "mondelinge geschiedenis dataset",
    "mondelinge geschiedenis transcriptie",
    # Narrative & Discourse compounds (English)
    "narrative analysis dataset",
    "narrative analysis transcript",
    "discourse analysis dataset",
    "discourse analysis qualitative data",
    "content analysis qualitative data",
    "life story dataset",
    "life story transcript",
    "oral history dataset",
    "oral history transcript",
    # Analytical Framework + Software compounds
    "grounded theory NVivo",
    "grounded theory ATLAS.ti",
    "grounded theory dataset",
    "grounded theory transcriptie",
    "gefundeerde theorie dataset",
    "thematische analyse dataset",
    "thematische analyse NVivo",
    "thematische codering dataset",
    "thematic analysis dataset",
    "thematic analysis NVivo",
    "thematic analysis transcript",
    "fenomenologie dataset",
    "fenomenologie transcriptie",
    "fenomenologie interview",
    "phenomenology dataset",
    "phenomenology transcript",
    "IPA transcript",
    "IPA dataset",
    "interpretatieve fenomenologische analyse dataset",
    "framework analyse dataset",
    "framework analysis dataset",
    # QDA Software + Repository compounds
    "NVivo DANS",
    "NVivo dataset",
    "NVivo transcriptie",
    "NVivo kwalitatieve data",
    "NVivo archief",
    "ATLAS.ti DANS",
    "ATLAS.ti dataset",
    "ATLAS.ti transcriptie",
    "ATLAS.ti kwalitatieve data",
    "MAXQDA dataset",
    "MAXQDA DANS",
    "MAXQDA transcriptie",
    "Dedoose dataset",
    "CAQDAS dataset",
    "CAQDAS DANS",
    "kwalitatieve analysesoftware dataset",
    # File Format + Method compounds
    "transcriptie kwalitatief onderzoek",
    "transcriptie kwalitatieve data",
    "transcriptie dataset",
    "transcriptie DANS",
    "transcriptie archief",
    "transcript qualitative research",
    "transcript qualitative data",
    "transcript DANS",
    "audiodata kwalitatief",
    "audiodata dataset",
    "geluidsopname kwalitatief",
    "geluidsopname dataset",
    "video-opname kwalitatief",
    "video-opname dataset",
    "videodata kwalitatief onderzoek",
    "codeboek kwalitatief",
    "codeboek dataset",
    "codeboek DANS",
    "codebook qualitative data",
    "codebook DANS",
    # Subject Domain + Method compounds
    "patiëntervaring interview",
    "patiëntervaring kwalitatief",
    "patiëntervaring focusgroep",
    "patiëntervaring dataset",
    "leefervaring interview",
    "leefervaring dataset",
    "geleefde ervaring transcriptie",
    "geleefde ervaring dataset",
    "geestelijke gezondheid kwalitatief",
    "geestelijke gezondheid interview",
    "geestelijke gezondheid focusgroep",
    "geestelijke gezondheid dataset",
    "armoede kwalitatief onderzoek",
    "armoede interview",
    "armoede levensverhaal",
    "migratie kwalitatief",
    "migratie interview",
    "migratie levensverhaal",
    "migratie dataset",
    "vluchteling interview",
    "vluchteling kwalitatief",
    "vluchteling dataset",
    "jeugd kwalitatief onderzoek",
    "jeugd focusgroep",
    "ouderen kwalitatief",
    "ouderen interview",
    "ouderen levensverhaal",
    "gezin kwalitatief onderzoek",
    "gezin interview",
    "werkervaring interview",
    "werkervaring kwalitatief",
    "lived experience transcript",
    "lived experience dataset",
    "mental health qualitative data",
    "mental health interview transcript",
    "patient experience qualitative data",
    "patient experience interview",
    # Repository Targeting compounds
    "DANS kwalitatieve data",
    "DANS transcriptie",
    "DANS interview",
    "DANS focusgroep",
    "DANS kwalitatief onderzoek",
    "EASY archief kwalitatief",
    "EASY archief transcriptie",
    "DataverseNL kwalitatief",
    "DataverseNL transcriptie",
    "DataverseNL interview",
    "onderzoeksdata kwalitatief",
    "onderzoeksdataset kwalitatief",
    "gearchiveerde data kwalitatief",
    "gedeponeerde data kwalitatief",
    "herbruikbare data kwalitatief",
    "data delen kwalitatief onderzoek",
    # Dutch Funder + Method compounds
    "NWO kwalitatief onderzoek",
    "NWO interview",
    "NWO focusgroep",
    "NWO transcriptie",
    "ZonMw kwalitatief",
    "ZonMw interview",
    "ZonMw focusgroep",
    "ZonMw dataset",
    "ZonMw transcriptie",
    "KNAW kwalitatief onderzoek",
    "SCP kwalitatief onderzoek",
    "WODC kwalitatief",
    "WODC interview",
    "promotieonderzoek kwalitatief",
    "promotieonderzoek interview",
    "proefschrift kwalitatieve data",
    "dissertatie kwalitatief",
    # Study Design + Evidence compounds
    "casestudie kwalitatief",
    "casestudie interview",
    "casestudie dataset",
    "meervoudige casestudie dataset",
    "longitudinaal kwalitatief dataset",
    "longitudinaal kwalitatief interview",
    "herhaald interview dataset",
    "gemengde methoden kwalitatieve data",
    "mixed methods qualitative data",
    "mixed methods transcript",
    "mixed methods NVivo",
    "secundaire analyse kwalitatieve data",
    "secondary analysis qualitative data",
    "triangulatie kwalitatieve data",
    "triangulation qualitative data",
    "doelgerichte steekproef interview",
    "sneeuwbalsteekproef interview",
    "theoretische saturatie dataset",
    # Experiential & Meaning-Making compounds
    "beleving interview",
    "beleving kwalitatief",
    "beleving dataset",
    "belevingsonderzoek dataset",
    "zingeving interview",
    "zingeving kwalitatief",
    "betekenisgeving interview",
    "betekenisgeving dataset",
    "ervaringsverhalen dataset",
    "persoonlijk verhaal dataset",
    "dagelijks leven kwalitatief",
    "alledaags leven interview",
]


# =========================================================
# CONSTANTS — ASSEMBLED FROM MASTER KEYWORD REGISTRY
# =========================================================

# --- QDA Software File Extensions ---
# DANS-official CAQDAS preferred/non-preferred formats (REFI-QDA, NVivo, ATLAS.ti, MAXQDA)
# plus audio, video, text and statistical formats accepted by DANS
QDA_QUALITATIVE_EXTENSIONS: Set[str] = {
    # CAQDAS / QDA project file formats
    "qdpx",       # REFI-QDA (DANS preferred)
    "nvpx",       # NVivo project (DANS non-preferred)
    "nvp",        # NVivo legacy
    "atlproj",    # ATLAS.ti copy bundle (DANS non-preferred)
    "atlasproj",  # ATLAS.ti alternate extension
    "mx",         # MAXQDA generic
    "mx2", "mx3", "mx4", "mx5", "mx11", "mx12", "mx18",
    "mx20", "mx22", "mx24", "mx24bac",
    "mex22", "mex24",
    "mqda", "mqbac", "mqex", "mqmtr", "mqtc",
    "mtr",
    "ppj", "pprj",  # QDA Miner
    "qdc", "qlt", "qpd",
    "f4p",          # f4analyse
    "hpr7",         # HyperRESEARCH
    "loa",          # Leximancer
    "m2k",          # MAXMaps
    "mc24",
    "mod",
    "sea",
    # Audio — interview recordings (DANS-official)
    "bwf",          # Broadcast Wave (preferred)
    "mxf",          # Material Exchange (preferred)
    "mka",          # Matroska Audio (preferred)
    "flac",         # FLAC (preferred)
    "opus",         # Opus (preferred)
    "wav",          # WAV (non-preferred)
    "mp3",          # MP3 (non-preferred)
    "aac",          # AAC (non-preferred)
    "m4a",          # AAC container (non-preferred)
    "aiff", "aif",  # AIFF (non-preferred)
    "ogg",          # OGG (non-preferred)
    # Video — interview / observation recordings (DANS-official)
    "mkv",          # Matroska Video (preferred)
    "mp4", "m4v",   # MPEG-4 (non-preferred)
    "mpg", "mpeg", "m2v",  # MPEG-2 (non-preferred)
    "avi",          # AVI (non-preferred)
    "mov", "qt",    # QuickTime (non-preferred)
    # Text / transcript formats (DANS-official)
    "txt",          # Plain text / Unicode transcript (preferred)
    "odt",          # OpenDocument Text (preferred)
    "pdf",          # PDF/A transcript (preferred)
    "docx",         # Office Open XML (non-preferred)
    "doc",          # MS Word legacy (non-preferred)
    "rtf",          # Rich Text (non-preferred)
    "xml",          # Coded XML transcript (preferred)
    "html",         # HTML (preferred)
    "md",           # Markdown (preferred)
    # Statistical / codebook formats (DANS-official)
    "sav",          # SPSS (non-preferred)
    "por",          # SPSS Portable (non-preferred)
    "sps",          # SPSS syntax (preferred)
    "dta",          # Stata (non-preferred)
    "jasp",         # JASP (non-preferred)
    "7dat", "sd2", "tpt",  # SAS (non-preferred)
    "csv",          # CSV (preferred — codebooks, coding exports)
}

# --- Description Keywords ---
# Full English + Dutch keyword vocabulary used for description-level matching.
# Any dataset whose description matches at least one term will pass the signal check.
DESCRIPTION_KEYWORDS: List[str] = sorted(set(
    _CORE_QDA_EN + _INTERVIEW_EN + _GROUP_EN + _ETHNOGRAPHY_EN +
    _NARRATIVE_EN + _FRAMEWORKS_EN + _DATA_TYPES_EN + _SOFTWARE_EN +
    _DISCIPLINES_EN + _APPLIED_EN + _STUDY_DESIGN_EN + _REPOSITORY_EN +
    _DANS_METADATA_FIELDS + _ACCESS_LICENCE +
    _CORE_QDA_NL + _INTERVIEW_NL + _GROUP_NL + _ETHNOGRAPHY_NL +
    _NARRATIVE_NL + _FRAMEWORKS_NL + _DATA_TYPES_NL + _DISCIPLINES_NL +
    _APPLIED_NL + _STUDY_DESIGN_NL + _REPOSITORY_NL +
    _INSTITUTIONS_NL + _FUNDERS_NL
))

# --- Filename Keywords ---
# Focused subset used for filename-level matching (file_name field on each file object).
# Kept tighter than description keywords to reduce false positives on filenames.
FILENAME_KEYWORDS: List[str] = [
    # English
    "qualitative", "interview", "interviews", "transcript", "transcripts",
    "focus group", "focus groups", "fieldnotes", "field notes", "oral history",
    "narrative", "codebook", "coding", "coded", "memo", "observation",
    "ethnograph", "biography", "life story", "verbatim", "recording",
    "audio", "video", "NVivo", "ATLAS", "MAXQDA", "CAQDAS",
    # Dutch
    "kwalitatief", "kwalitatieve", "interview", "transcriptie", "transcript",
    "focusgroep", "focusgroepen", "veldnotities", "veldaantekeningen",
    "levensverhaal", "narratief", "codeboek", "codering", "observatie",
    "etnografie", "biografie", "opname", "geluidsopname", "dagboek",
    "topiclijst", "interviewleidraad",
]

# --- API Search Terms ---
# All single-keyword and compound terms sent to the Dataverse search API.
# The pipeline iterates over every term and de-duplicates results by global_id.
ALL_API_SEARCH_TERMS: List[str] = list(dict.fromkeys(
    # Core single terms (English + Dutch) — high-recall API queries
    [
        "qualitative", "interview", "transcript", "focus group", "ethnography",
        "oral history", "narrative", "grounded theory", "thematic analysis",
        "phenomenology", "life story", "NVivo", "ATLAS.ti", "MAXQDA", "CAQDAS",
        "REFI-QDA", "codebook", "verbatim", "fieldwork", "field notes",
        "mixed methods", "case study", "lived experience", "discourse analysis",
        "content analysis", "participatory research",
        # Dutch core
        "kwalitatief", "kwalitatieve data", "kwalitatief onderzoek",
        "diepte-interview", "focusgroep", "transcriptie", "levensverhaal",
        "narratief", "etnografie", "veldwerk", "veldnotities", "observatie",
        "gefundeerde theorie", "thematische analyse", "fenomenologie",
        "discoursanalyse", "inhoudsanalyse", "beleving", "zingeving",
        "betekenisgeving", "mondelinge geschiedenis", "codeboek",
        "gemengde methoden", "casestudie", "participerende observatie",
    ] + _COMPOUNDS
))

ALLOWED_FILE_STATUSES: Set[str] = {
    "SUCCEEDED",
    "FAILED_SERVER_UNRESPONSIVE",
    "FAILED_LOGIN_REQUIRED",
    "FAILED_TOO_LARGE",
}


# =========================================================
# PIPELINE CONFIG — USES FULL KEYWORD LIST
# =========================================================
CONFIG = PipelineConfig(query=ALL_API_SEARCH_TERMS)


# =========================================================
# LOGGING SETUP
# =========================================================
logger = logging.getLogger(__name__)


def setup_logging(level: int = CONFIG.log_level) -> None:
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=level,
            format=CONFIG.log_format,
            datefmt=CONFIG.log_date_format,
        )
    else:
        root_logger.setLevel(level)


# =========================================================
# GENERAL HELPERS
# =========================================================
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_directory(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            text = normalize_text(item)
            if text:
                parts.append(text)
        return " ".join(parts).lower().strip()
    if isinstance(value, dict):
        if "value" in value:
            return normalize_text(value["value"])
        return " ".join(str(v) for v in value.values() if v is not None).lower().strip()
    return str(value).lower().strip()


def safe_folder_name(name: str) -> str:
    name = clean_text(name).lower()
    name = re.sub(r"[\\/]+", "-", name)
    name = re.sub(r"[^a-z0-9._-]+", "-", name)
    return name.strip("-_")


def safe_filename(name: str) -> str:
    name = clean_text(name)
    return re.sub(r"[\\/]+", "_", name)


def get_file_extension(filename: str) -> str:
    if not filename or "." not in filename:
        return "unknown"
    return filename.rsplit(".", 1)[-1].lower().strip()


def make_project_key(repository_id: int, project_id: int) -> int:
    return int(f"{repository_id}{project_id}")


def get_search_terms(query: Optional[List[str]]) -> List[str]:
    if query is None or query == []:
        return ["*"]
    if isinstance(query, str):
        return [query]
    return list(query)


def remove_directory_if_exists(path: str) -> None:
    if path and os.path.exists(path):
        shutil.rmtree(path)


def build_temp_project_root(config: PipelineConfig, project: Dict[str, Any]) -> str:
    ensure_directory(config.repo_temp_dir)
    unique_suffix = uuid.uuid4().hex
    temp_name = f"{project['download_project_folder']}__{project['project_id']}__{unique_suffix}"
    return os.path.join(config.repo_temp_dir, temp_name)


def build_final_project_root(config: PipelineConfig, project_folder_name: str) -> str:
    return os.path.join(config.repo_dir, project_folder_name)


def build_removal_candidates(
    config: PipelineConfig,
    current_project_folder_name: str,
    existing_project_folder_name: Optional[str],
) -> List[str]:
    candidates: List[str] = []
    current_root = build_final_project_root(config, current_project_folder_name)
    candidates.append(current_root)
    if existing_project_folder_name and existing_project_folder_name != current_project_folder_name:
        candidates.append(build_final_project_root(config, existing_project_folder_name))
    deduped: List[str] = []
    for path in candidates:
        if path not in deduped:
            deduped.append(path)
    return deduped


def replace_project_folder_from_temp(
    config: PipelineConfig,
    current_project_folder_name: str,
    existing_project_folder_name: Optional[str],
    temp_project_root: str,
) -> str:
    removal_candidates = build_removal_candidates(
        config=config,
        current_project_folder_name=current_project_folder_name,
        existing_project_folder_name=existing_project_folder_name,
    )
    for path in removal_candidates:
        remove_directory_if_exists(path)
    final_project_root = build_final_project_root(config, current_project_folder_name)
    ensure_directory(config.repo_dir)
    shutil.move(temp_project_root, final_project_root)
    return final_project_root


# =========================================================
# REQUEST SESSION
# =========================================================
def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    })
    return session


# =========================================================
# DATABASE LAYER
# =========================================================
class DatabaseManager:

    def __init__(self, db_path: str, schema_path: str) -> None:
        self.db_path = db_path
        self.schema_path = schema_path
        self.conn = sqlite3.connect(self.db_path)
        self.cur = self.conn.cursor()

    def initialize_schema(self) -> None:
        with open(self.schema_path, "r", encoding="utf-8") as f:
            self.cur.executescript(f.read())
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def get_existing_project_download_folder(self, project_key: int) -> Optional[str]:
        self.cur.execute(
            "SELECT download_project_folder FROM projects WHERE project_key = ?",
            (project_key,),
        )
        row = self.cur.fetchone()
        if row and row[0]:
            return str(row[0])
        return None

    def clear_project_rows(self, project_key: int) -> None:
        self.cur.execute("DELETE FROM files WHERE project_key = ?", (project_key,))
        self.cur.execute("DELETE FROM project_versions WHERE project_key = ?", (project_key,))
        self.cur.execute("DELETE FROM keywords WHERE project_key = ?", (project_key,))
        self.cur.execute("DELETE FROM person_role WHERE project_key = ?", (project_key,))
        self.cur.execute("DELETE FROM licenses WHERE project_key = ?", (project_key,))
        self.cur.execute("DELETE FROM failures WHERE project_key = ?", (project_key,))
        self.cur.execute("DELETE FROM projects WHERE project_key = ?", (project_key,))

    def save_project(self, project: Dict[str, Any]) -> None:
        self.cur.execute("""
            INSERT OR REPLACE INTO projects (
                project_key, project_id, query_string, repository_id, repository_url, project_url,
                title, description, language, doi, upload_date, download_date,
                download_repository_folder, download_project_folder, download_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            project["project_key"], project["project_id"], project["query_string"],
            project["repository_id"], project["repository_url"], project["project_url"],
            project["title"], project["description"], project["language"], project["doi"],
            project["upload_date"], project["download_date"],
            project["download_repository_folder"], project["download_project_folder"],
            project["download_method"],
        ))
        for kw in project.get("keywords", []):
            self.cur.execute(
                "INSERT INTO keywords (project_key, keyword) VALUES (?, ?)",
                (project["project_key"], kw),
            )
        for name in sorted(set(project.get("authors", []))):
            self.cur.execute(
                "INSERT INTO person_role (project_key, name, role) VALUES (?, ?, ?)",
                (project["project_key"], name, "AUTHOR"),
            )
        for name in sorted(set(project.get("contacts", []))):
            self.cur.execute(
                "INSERT INTO person_role (project_key, name, role) VALUES (?, ?, ?)",
                (project["project_key"], name, "CONTACT"),
            )
        for name in sorted(set(project.get("producers", []))):
            self.cur.execute(
                "INSERT INTO person_role (project_key, name, role) VALUES (?, ?, ?)",
                (project["project_key"], name, "PRODUCER"),
            )
        for name in sorted(set(project.get("contributors", []))):
            self.cur.execute(
                "INSERT INTO person_role (project_key, name, role) VALUES (?, ?, ?)",
                (project["project_key"], name, "CONTRIBUTOR"),
            )
        if project.get("license"):
            self.cur.execute(
                "INSERT INTO licenses (project_key, license) VALUES (?, ?)",
                (project["project_key"], project["license"]),
            )

    def save_project_version(self, version_meta: Dict[str, Any]) -> int:
        self.cur.execute("""
            INSERT INTO project_versions (
                project_key, version_id, version, version_state,
                publication_date, release_time, download_date, download_version_folder
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            version_meta["project_key"], version_meta["version_id"], version_meta["version"],
            version_meta["version_state"], version_meta["publication_date"],
            version_meta["release_time"], version_meta["download_date"],
            version_meta["download_version_folder"],
        ))
        return int(self.cur.lastrowid)

    def save_files(
        self,
        project_key: int,
        version_key: int,
        files: List[Dict[str, Any]],
        download_statuses: Dict[str, str],
    ) -> None:
        for idx, file_info in enumerate(files, 1):
            file_name = file_info.get("file_name") or ""
            key = f"{idx}:{safe_filename(file_name or 'download.bin')}"
            status = download_statuses.get(key, "FAILED_SERVER_UNRESPONSIVE")
            if status not in ALLOWED_FILE_STATUSES:
                status = "FAILED_SERVER_UNRESPONSIVE"
            self.cur.execute("""
                INSERT INTO files (project_key, version_key, file_name, file_type, status)
                VALUES (?, ?, ?, ?, ?)
            """, (
                project_key, version_key, file_name,
                file_info.get("file_type") or "unknown", status,
            ))


# =========================================================
# DATAVERSE FIELD EXTRACTION HELPERS
# =========================================================
def get_field_value(fields: List[Dict[str, Any]], type_name: str) -> Any:
    for field in fields:
        if field.get("typeName") == type_name:
            return field.get("value")
    return None


def extract_compound_values(value: Any, key: str) -> List[str]:
    results: List[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                inner = item.get(key, {})
                if isinstance(inner, dict):
                    nested_value = inner.get("value")
                    if nested_value:
                        results.append(str(nested_value).strip())
    return results


def get_first_text(fields: List[Dict[str, Any]], type_name: str) -> str:
    value = get_field_value(fields, type_name)
    if isinstance(value, list):
        flat: List[str] = []
        for item in value:
            if isinstance(item, dict) and "value" in item:
                flat.extend([str(x).strip() for x in item["value"] if x])
            elif item:
                flat.append(str(item).strip())
        return " ".join([x for x in flat if x]).strip()
    if value is None:
        return ""
    return str(value).strip()


def get_title(fields: List[Dict[str, Any]]) -> str:
    return get_first_text(fields, "title")


def get_description(fields: List[Dict[str, Any]]) -> str:
    values = get_field_value(fields, "dsDescription")
    descriptions: List[str] = []
    if isinstance(values, list):
        for item in values:
            if not isinstance(item, dict):
                continue
            text = item.get("dsDescriptionValue", {}).get("value")
            if text:
                descriptions.append(str(text).strip())
    return " ".join(descriptions).strip()


def get_authors(fields: List[Dict[str, Any]]) -> List[str]:
    return extract_compound_values(get_field_value(fields, "author"), "authorName")


def get_contacts(fields: List[Dict[str, Any]]) -> List[str]:
    return extract_compound_values(get_field_value(fields, "datasetContact"), "datasetContactName")


def get_producers(fields: List[Dict[str, Any]]) -> List[str]:
    return extract_compound_values(get_field_value(fields, "producer"), "producerName")


def get_contributors(fields: List[Dict[str, Any]]) -> List[str]:
    return extract_compound_values(get_field_value(fields, "contributor"), "contributorName")


def get_keywords(fields: List[Dict[str, Any]]) -> List[str]:
    keyword_values = get_field_value(fields, "keyword")
    keywords: List[str] = []
    if isinstance(keyword_values, list):
        for item in keyword_values:
            if not isinstance(item, dict):
                continue
            kw = item.get("keywordValue", {}).get("value")
            if kw:
                keywords.append(str(kw).strip())
    return sorted(set([x for x in keywords if x]))


def get_language(fields: List[Dict[str, Any]]) -> str:
    return get_first_text(fields, "language")


def get_license(latest_version: Dict[str, Any]) -> str:
    license_obj = latest_version.get("license") or {}
    return str(license_obj.get("name") or "").strip()


def get_version_label(version_obj: Dict[str, Any]) -> str:
    version_number = version_obj.get("versionNumber")
    version_minor = version_obj.get("versionMinorNumber")
    if version_number is None:
        return ""
    if version_minor is None:
        return str(version_number)
    return f"{version_number}.{version_minor}"


# =========================================================
# API LAYER
# =========================================================
def fetch_search_page(session: requests.Session, config: PipelineConfig, start: int, query_string: str) -> Dict[str, Any]:
    params = {
        "q": query_string,
        "type": "dataset",
        "start": start,
        "per_page": config.per_page,
    }
    response = session.get(config.search_url, params=params, timeout=config.timeout)
    response.raise_for_status()
    return response.json()


def fetch_dataset(session: requests.Session, config: PipelineConfig, persistent_id: str) -> Dict[str, Any]:
    params = {"persistentId": persistent_id}
    response = session.get(config.dataset_url, params=params, timeout=config.timeout)
    response.raise_for_status()
    return response.json()


def list_dataset_versions(session: requests.Session, config: PipelineConfig, dataset_id: int) -> List[Dict[str, Any]]:
    url = f"{config.base_url}/datasets/{dataset_id}/versions"
    response = session.get(url, timeout=config.timeout)
    response.raise_for_status()
    data = response.json()
    return data.get("data", []) or []


# =========================================================
# QUALITATIVE SIGNAL DETECTION
# =========================================================
def detect_description_keywords(description: Any) -> Tuple[bool, str]:
    """
    Detect whether a dataset description contains any of the master keyword vocabulary.
    Uses the full DESCRIPTION_KEYWORDS list (933-term master registry).
    """
    text = normalize_text(description)
    if not text:
        return False, "Description missing"
    matches = [kw for kw in DESCRIPTION_KEYWORDS if kw.lower() in text]
    if matches:
        return True, f"Description matched: {', '.join(sorted(set(matches[:10])))}"
    return False, "No qualitative keywords found in description"


def detect_files_signals(files: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Detect qualitative file evidence using:
    - Known QDA/audio/video/text/statistical extensions (QDA_QUALITATIVE_EXTENSIONS)
    - Filename keywords (FILENAME_KEYWORDS)
    """
    extensions_found: Set[str] = set()
    keyword_hits: Set[str] = set()

    for file_meta in files:
        filename = str(file_meta.get("file_name") or "")
        lower_name = filename.lower()

        extension = get_file_extension(filename)
        if extension in QDA_QUALITATIVE_EXTENSIONS:
            extensions_found.add(extension)

        for keyword in FILENAME_KEYWORDS:
            if keyword.lower() in lower_name:
                keyword_hits.add(keyword)

    extension_found = len(extensions_found) > 0
    filename_keyword_found = len(keyword_hits) > 0

    if extension_found and filename_keyword_found:
        reason = (
            f"Matched extension(s): {', '.join(sorted(extensions_found))}; "
            f"matched filename keyword(s): {', '.join(sorted(keyword_hits))}"
        )
    elif extension_found:
        reason = f"Matched extension(s): {', '.join(sorted(extensions_found))}"
    elif filename_keyword_found:
        reason = f"Matched filename keyword(s): {', '.join(sorted(keyword_hits))}"
    else:
        reason = "No qualitative file signal found"

    return {
        "extension_found": extension_found,
        "qualitative_keyword_found": filename_keyword_found,
        "reason": reason,
    }


# =========================================================
# METADATA EXTRACTION
# =========================================================
def extract_files_from_version(version_obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for version_file in version_obj.get("files", []) or []:
        data_file = version_file.get("dataFile", {}) or {}
        filename = data_file.get("filename") or version_file.get("label") or ""
        content_type = data_file.get("contentType") or ""
        file_id = data_file.get("id")
        output.append({
            "file_id": file_id,
            "file_name": filename,
            "file_type": get_file_extension(filename),
            "restricted": bool(version_file.get("restricted")),
            "content_type": content_type,
            "file_access_request": bool(data_file.get("fileAccessRequest")),
        })
    return output


def extract_project_metadata(dataset_json: Dict[str, Any], config: PipelineConfig, query_string: str) -> Dict[str, Any]:
    data = dataset_json["data"]
    latest_version = data["latestVersion"]
    metadata_blocks = latest_version.get("metadataBlocks", {}) or {}
    citation_fields = metadata_blocks.get("citation", {}).get("fields", [])
    project_id = int(data["id"])
    project_key = make_project_key(config.repository_id, project_id)
    identifier = data.get("identifier") or ""
    persistent_url = data.get("persistentUrl") or ""
    return {
        "project_key": project_key,
        "project_id": project_id,
        "query_string": query_string,
        "repository_id": config.repository_id,
        "repository_url": config.repository_url,
        "project_url": persistent_url,
        "title": get_title(citation_fields),
        "description": get_description(citation_fields),
        "language": get_language(citation_fields),
        "doi": persistent_url,
        "upload_date": latest_version.get("publicationDate") or data.get("publicationDate") or "",
        "download_date": now_utc_iso(),
        "download_repository_folder": config.repo_name,
        "download_project_folder": safe_folder_name(identifier),
        "download_method": config.download_method,
        "identifier": identifier,
        "keywords": get_keywords(citation_fields),
        "authors": get_authors(citation_fields),
        "contacts": get_contacts(citation_fields),
        "producers": get_producers(citation_fields),
        "contributors": get_contributors(citation_fields),
        "license": get_license(latest_version),
    }


def extract_version_metadata(version_obj: Dict[str, Any], project_key: int) -> Dict[str, Any]:
    version_label = get_version_label(version_obj)
    return {
        "project_key": project_key,
        "version_id": version_obj.get("id"),
        "version": version_label,
        "version_state": version_obj.get("versionState") or "",
        "publication_date": version_obj.get("publicationDate") or "",
        "release_time": version_obj.get("releaseTime") or "",
        "download_date": now_utc_iso(),
        "download_version_folder": f"v{version_label}" if version_label else "",
    }


# =========================================================
# DOWNLOAD LAYER
# =========================================================
def download_file(session: requests.Session, config: PipelineConfig, url: str, path: str) -> str:
    try:
        ensure_directory(os.path.dirname(path))
        with session.get(url, stream=True, timeout=config.timeout) as response:
            if response.status_code in (401, 403):
                return "FAILED_LOGIN_REQUIRED"
            if response.status_code == 413:
                return "FAILED_TOO_LARGE"
            if response.status_code != 200:
                return "FAILED_SERVER_UNRESPONSIVE"
            with open(path, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        return "SUCCEEDED"
    except requests.RequestException:
        return "FAILED_SERVER_UNRESPONSIVE"
    except Exception:
        return "FAILED_SERVER_UNRESPONSIVE"


def download_version_files_to_root(
    session: requests.Session,
    config: PipelineConfig,
    project_root: str,
    version_label: str,
    files: List[Dict[str, Any]],
) -> Dict[str, str]:
    version_root = os.path.join(project_root, f"v{version_label}")
    ensure_directory(version_root)
    statuses: Dict[str, str] = {}
    for idx, file_info in enumerate(files, 1):
        file_id = file_info.get("file_id")
        filename = safe_filename(file_info.get("file_name") or "download.bin")
        if not file_id:
            statuses[f"{idx}:{filename}"] = "FAILED_SERVER_UNRESPONSIVE"
            continue
        target_path = os.path.join(version_root, filename)
        download_url = f"{config.base_url}/access/datafile/{file_id}?format=original"
        status = download_file(session, config, download_url, target_path)
        if status not in ALLOWED_FILE_STATUSES:
            status = "FAILED_SERVER_UNRESPONSIVE"
        statuses[f"{idx}:{filename}"] = status
    return statuses


# =========================================================
# SUMMARY HELPERS
# =========================================================
def initialize_summary(search_terms: List[str]) -> Dict[str, Any]:
    return {
        "counts": {
            "total_projects": 0,
            "projects_with_files": 0,
            "projects_with_no_files": 0,
            "identified_qualitative_projects": 0,
            "identified_qualitative_with_files": 0,
            "identified_qualitative_with_no_files": 0,
            "identified_non_qualitative_projects": 0,
            "identified_non_qualitative_with_files": 0,
            "identified_non_qualitative_with_no_files": 0,
        },
        "project_ids": {
            "identified_qualitative_with_files": [],
            "identified_qualitative_with_no_files": [],
            "identified_non_qualitative_with_files": [],
            "identified_non_qualitative_with_no_files": [],
        },
        "searched_terms": search_terms,
        "generated_at_utc": "",
    }


def classify_project_bucket(is_qualitative: bool, has_files: bool) -> str:
    if is_qualitative and has_files:
        return "identified_qualitative_with_files"
    if is_qualitative and not has_files:
        return "identified_qualitative_with_no_files"
    if not is_qualitative and has_files:
        return "identified_non_qualitative_with_files"
    return "identified_non_qualitative_with_no_files"


def update_summary(summary: Dict[str, Any], project_id: int, is_qualitative: bool, has_files: bool) -> None:
    counts = summary["counts"]
    project_ids = summary["project_ids"]
    counts["total_projects"] += 1
    if has_files:
        counts["projects_with_files"] += 1
    else:
        counts["projects_with_no_files"] += 1
    if is_qualitative:
        counts["identified_qualitative_projects"] += 1
        if has_files:
            counts["identified_qualitative_with_files"] += 1
        else:
            counts["identified_qualitative_with_no_files"] += 1
    else:
        counts["identified_non_qualitative_projects"] += 1
        if has_files:
            counts["identified_non_qualitative_with_files"] += 1
        else:
            counts["identified_non_qualitative_with_no_files"] += 1
    bucket = classify_project_bucket(is_qualitative, has_files)
    project_ids[bucket].append(project_id)


def save_summary_json(summary: Dict[str, Any], output_path: str) -> None:
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)


def log_project_decision(
    project_id: int,
    action: str,
    description_signal: bool,
    file_extension_signal: bool,
    file_name_kw_signal: bool,
    has_files: bool,
    qualitative: bool,
) -> None:
    logger.info(
        "Project %s | %s | description_signal=%s | file_extension_signal=%s | "
        "file_name_kw_signal=%s | has_files=%s | qualitative=%s",
        project_id, action, description_signal, file_extension_signal,
        file_name_kw_signal, has_files, qualitative,
    )


# =========================================================
# MAIN PIPELINE
# =========================================================
def run_pipeline(config: PipelineConfig) -> Dict[str, Any]:
    """
    Execute the full DANS Dataverse qualitative-identification pipeline.

    Uses the 933-term master keyword registry across three detection layers:
      1. API query layer     — ALL_API_SEARCH_TERMS sent to ssh.datastations.nl/api/search
      2. Description layer   — DESCRIPTION_KEYWORDS matched against dataset description text
      3. File signal layer   — QDA_QUALITATIVE_EXTENSIONS + FILENAME_KEYWORDS on each file

    A project is classified qualitative when:
      - description_signal AND (extension_signal OR filename_keyword_signal)

    Refresh-safe: every encountered project is re-evaluated from live source.
    """
    logger.info("Starting DANS Dataverse pipeline")
    logger.info(
        "Keyword registry: %d API search terms | %d description keywords | "
        "%d filename keywords | %d file extensions",
        len(ALL_API_SEARCH_TERMS),
        len(DESCRIPTION_KEYWORDS),
        len(FILENAME_KEYWORDS),
        len(QDA_QUALITATIVE_EXTENSIONS),
    )

    ensure_directory(config.repo_dir)
    ensure_directory(config.repo_temp_dir)

    session = build_session()
    db = DatabaseManager(config.db_path, config.schema_path)
    db.initialize_schema()

    seen: Set[str] = set()
    search_terms = get_search_terms(config.query)
    summary = initialize_summary(search_terms)

    try:
        for query in search_terms:
            logger.info("Searching query: %s", query if query != "*" else "ALL DATASETS")
            start = 0

            while True:
                try:
                    search_data = fetch_search_page(session, config, start, query)
                except Exception as exc:
                    logger.warning("Search failed for query=%s start=%s: %s", query, start, exc)
                    break

                data = search_data.get("data", {})
                items = data.get("items", [])
                total_count = data.get("total_count", 0)

                if not items:
                    logger.info("No items returned for query=%s start=%s; stopping query", query, start)
                    break

                logger.info(
                    "Fetched %s items at start=%s / total_count=%s for query=%s",
                    len(items), start, total_count, query,
                )

                for item in items:
                    global_id = item.get("global_id")
                    if not global_id or global_id in seen:
                        continue
                    seen.add(global_id)

                    temp_project_root: Optional[str] = None

                    try:
                        dataset_json = fetch_dataset(session, config, global_id)
                        project = extract_project_metadata(dataset_json, config, query_string=query)
                        existing_project_folder_name = db.get_existing_project_download_folder(project["project_key"])

                        desc_ok, desc_reason = detect_description_keywords(project.get("description"))

                        dataset_id = int(dataset_json["data"]["id"])
                        versions = list_dataset_versions(session, config, dataset_id)

                        version_blocks: List[Dict[str, Any]] = []
                        project_has_files = False
                        any_extension_signal = False
                        any_filename_kw_signal = False

                        for version_obj in versions:
                            version_label = get_version_label(version_obj)
                            if not version_label:
                                continue

                            version_files = extract_files_from_version(version_obj)
                            if version_files:
                                project_has_files = True

                            file_signal = detect_files_signals(version_files)
                            version_blocks.append({
                                "version_label": version_label,
                                "version_meta": extract_version_metadata(version_obj, project["project_key"]),
                                "files": version_files,
                                "file_signal": file_signal,
                                "download_statuses": {},
                            })

                            if file_signal["extension_found"]:
                                any_extension_signal = True
                            if file_signal["qualitative_keyword_found"]:
                                any_filename_kw_signal = True

                            logger.debug(
                                "Project %s | Version v%s | extension_found=%s | filename_keyword_found=%s | %s",
                                project["project_id"], version_label,
                                file_signal["extension_found"],
                                file_signal["qualitative_keyword_found"],
                                file_signal["reason"],
                            )

                        is_qualitative_project = desc_ok and (any_extension_signal or any_filename_kw_signal)

                        update_summary(
                            summary=summary,
                            project_id=project["project_id"],
                            is_qualitative=is_qualitative_project,
                            has_files=project_has_files,
                        )

                        logger.debug(
                            "Project %s | title=%s | description_reason=%s",
                            project["project_id"], project["title"], desc_reason,
                        )

                        if not project_has_files:
                            db.clear_project_rows(project["project_key"])
                            for path in build_removal_candidates(
                                config=config,
                                current_project_folder_name=project["download_project_folder"],
                                existing_project_folder_name=existing_project_folder_name,
                            ):
                                remove_directory_if_exists(path)
                            db.commit()
                            log_project_decision(
                                project_id=project["project_id"], action="skipped",
                                description_signal=desc_ok, file_extension_signal=any_extension_signal,
                                file_name_kw_signal=any_filename_kw_signal,
                                has_files=False, qualitative=is_qualitative_project,
                            )
                            continue

                        if not is_qualitative_project:
                            db.clear_project_rows(project["project_key"])
                            for path in build_removal_candidates(
                                config=config,
                                current_project_folder_name=project["download_project_folder"],
                                existing_project_folder_name=existing_project_folder_name,
                            ):
                                remove_directory_if_exists(path)
                            db.commit()
                            log_project_decision(
                                project_id=project["project_id"], action="skipped",
                                description_signal=desc_ok, file_extension_signal=any_extension_signal,
                                file_name_kw_signal=any_filename_kw_signal,
                                has_files=True, qualitative=False,
                            )
                            continue

                        temp_project_root = build_temp_project_root(config, project)
                        ensure_directory(temp_project_root)

                        for block in version_blocks:
                            block["download_statuses"] = download_version_files_to_root(
                                session=session, config=config,
                                project_root=temp_project_root,
                                version_label=block["version_label"],
                                files=block["files"],
                            )

                        final_project_root = replace_project_folder_from_temp(
                            config=config,
                            current_project_folder_name=project["download_project_folder"],
                            existing_project_folder_name=existing_project_folder_name,
                            temp_project_root=temp_project_root,
                        )
                        temp_project_root = None

                        logger.debug(
                            "Project %s | replaced local folder at %s",
                            project["project_id"], final_project_root,
                        )

                        db.clear_project_rows(project["project_key"])
                        db.save_project(project)

                        for block in version_blocks:
                            version_key = db.save_project_version(block["version_meta"])
                            db.save_files(
                                project_key=project["project_key"],
                                version_key=version_key,
                                files=block["files"],
                                download_statuses=block["download_statuses"],
                            )

                        db.commit()

                        log_project_decision(
                            project_id=project["project_id"], action="saved",
                            description_signal=desc_ok, file_extension_signal=any_extension_signal,
                            file_name_kw_signal=any_filename_kw_signal,
                            has_files=True, qualitative=True,
                        )

                    except Exception as exc:
                        db.rollback()
                        logger.exception("Failed processing dataset global_id=%s: %s", global_id, exc)
                    finally:
                        if temp_project_root and os.path.exists(temp_project_root):
                            remove_directory_if_exists(temp_project_root)

                start += config.per_page
                if start >= total_count:
                    break

        summary["generated_at_utc"] = now_utc_iso()
        save_summary_json(summary, config.summary_path)

        logger.info("Saved summary JSON: %s", config.summary_path)
        logger.info("Pipeline summary: %s", json.dumps(summary, ensure_ascii=False))

        return summary

    finally:
        db.close()
        session.close()


# =========================================================
# ENTRY POINT
# =========================================================
if __name__ == "__main__":
    setup_logging()
    try:
        run_pipeline(CONFIG)
    except Exception:
        logger.exception("Pipeline execution failed")
        raise