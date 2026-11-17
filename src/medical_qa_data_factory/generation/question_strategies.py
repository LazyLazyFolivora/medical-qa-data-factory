"""Generation policies select evidence and composition; they do not grade output."""
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class QuestionStrategy:
    name: str
    retrieval_goal: str
    design_steps: tuple[str, ...]
    missing_material_action: str
    preferred_sources: tuple[str, ...]

    def payload(self):
        return {"version": "question-strategies-v2-research-first", **asdict(self)}


STRATEGIES = {
    "easy": QuestionStrategy(
        "easy",
        "Find a precise, useful, directly documented fact about the named entity. Prefer the authoritative source for the requested property.",
        (
            "Select one complete supported observation, including species and necessary conditions.",
            "Ask one natural, bounded question with a directly supported answer. Avoid trivia that merely copies an internal database identifier.",
            "Keep the public question self-contained. Do not add unrelated facts to increase complexity.",
        ),
        "Find the authoritative missing property or switch to another useful directly documented observation.",
        ("ncbi_gene", "hgnc", "ensembl", "pubmed", "primekg"),
    ),
    "medium": QuestionStrategy(
        "medium",
        "Find connected facts supporting a meaningful entity resolution followed by a relation lookup, or a useful condition-based selection. Seek the missing relation after identifying the seed.",
        (
            "Read concrete observations first; compare their conditions and outcomes, then abstract one useful information need from a supported connection or discriminating condition.",
            "Describe the necessary intermediate entity or condition and the evidence connecting it to the answer.",
            "Check whether a direct record already answers the question. Do not hide an easy lookup behind an obscure identifier.",
            "Write a natural question with all necessary scope visible. Keep useful direct questions as supplementary output.",
        ),
        "State the missing relation or discriminating condition and search for it. Do not keep collecting equivalent identity fields.",
        ("primekg", "pubmed", "ncbi_gene", "hgnc", "ensembl"),
    ),
    "hard": QuestionStrategy(
        "hard",
        "Use the seed/graph to discover promising research leads, then read original results and compare conditions, controls and endpoints. Graph neighbors are leads, not a checklist to exhaust. Find connected evidence before inventing a difficult question. A single study may support a domain question, but extracting or comparing that study's sample results is not itself the information need. Keep necessary domain conditions without making the paper or its experimental procedure the question's subject.",
        (
            "Read complete source observations, compare conditions/controls/endpoints, and identify a meaningful difference or connection worth explaining.",
            "Form a bounded integrated understanding, then abstract ONE information need. Preserve source-supported conditions; do not manufacture a mechanism from an observed difference.",
            "Map the shortest reasonable solution path and identify what evidence each necessary step contributes.",
            "Verify that the connections preserve entity identity, species, population and experimental conditions; never infer causation from adjacent graph edges alone.",
            "Check shortcuts and alternatives. More sources proving the same fact, longer wording and obscure terms do not create difficulty.",
            "Write the final standalone question and expected answer from supported facts. Preserve lower-level valid questions as supplementary output without presenting them as hard.",
        ),
        "Return the precise missing relationship, comparison candidate or exclusion evidence in insufficiency_reason and fact-selection information_gain. Explore that gap before proposing more isolated attributes.",
        ("primekg", "pubmed", "ncbi_gene", "ensembl", "hgnc"),
    ),
    "free": QuestionStrategy(
        "free",
        "Explore useful supported observations about the entity without imposing a target difficulty. Follow promising evidence and reader needs.",
        (
            "Read a complete observation with its actors, intervention, result and conditions.",
            "Add another supported fact only if it contributes useful information to the same reader need.",
            "Choose a direct question, connected question or comparison as the evidence permits; report insufficiency when needed.",
            "Write the minimum sufficient answer and a natural standalone question. Let the independent assessor determine actual difficulty.",
        ),
        "Follow the most promising new factual direction or report the concrete evidence gap.",
        ("pubmed", "primekg", "ncbi_gene", "hgnc", "ensembl"),
    ),
}


def question_strategy(goal):
    data = goal if isinstance(goal, dict) else goal.model_dump()
    selected = data.get("generation_strategy", "auto")
    if selected == "auto":
        targets = list(dict.fromkeys(data.get("difficulty_targets", [])))
        selected = targets[0] if len(targets) == 1 else "free"
    if selected not in STRATEGIES:
        raise ValueError(f"unknown question generation strategy: {selected}")
    return STRATEGIES[selected]
