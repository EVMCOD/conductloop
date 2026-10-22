"""Fallo 2 de los evals (#28): the memory heuristic must recognise Spanish
fact cues, not only English verbs."""
from __future__ import annotations

import pytest

from ops_dashboard import memory_ai


@pytest.mark.parametrize("sentence", [
    "ConductLoop usa SQLite para guardar los datos en local.",
    "Prefiero respuestas cortas y en español.",
    "Ana prefiere el modo oscuro en la app.",
    "Decidimos fijar el plan Pro en 29 € al mes.",
    "Hemos decidido mover el backend a Cloudflare.",
    "El proyecto se llama ConductLoop desde marzo.",
    "La empresa tiene su sede y está en Valencia.",
    "Trabajo con Python y TypeScript a diario.",
    "Nunca despliegues los viernes por la tarde.",
    "Me gusta revisar las métricas por la mañana.",
    "El servidor corre en un Mac mini de la oficina.",
    "Eligió Stripe como pasarela de pagos.",
    "Soy vegetariano desde hace años.",
    "Estamos en Valencia desde 2020.",
    "Somos tres personas en el equipo.",
    "Estoy en la zona horaria de Madrid.",
])
def test_spanish_fact_cues_are_proposed(sentence):
    assert memory_ai.propose_from_text(sentence) == [sentence]


@pytest.mark.parametrize("sentence", [
    "Gracias por la ayuda con todo esto.",
    "Vale, lo miro mañana por la tarde.",
    "¿Puedes revisar el último correo?",
])
def test_spanish_chatter_without_cues_is_ignored(sentence):
    assert memory_ai.propose_from_text(sentence) == []


def test_english_cues_unchanged():
    assert memory_ai.propose_from_text("The engine uses SQLite for storage.") == ["The engine uses SQLite for storage."]
    assert memory_ai.propose_from_text("Thanks a lot for the help today!") == []


def test_cue_must_be_a_whole_word():
    # "es" inside "estructura"/"interesante" must not count as the verb "es".
    assert memory_ai.propose_from_text("Una estructura interesante para el informe.") == []
