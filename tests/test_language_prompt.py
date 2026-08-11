from benji_api.agents.prompts import build_benji_instructions
from benji_api.agents.prompts.group import build_group_module
from benji_api.agents.prompts.language import build_language_module
from benji_api.models.user import User


def test_direct_language_module_uses_persistent_private_preference() -> None:
    user = User(phone_number="+14155552671")
    user.preferred_language_mode = "egyptian_franco"

    module = build_language_module(user)
    normalized = " ".join(module.content.split())

    assert module.name == "language_style"
    assert "persistent preferred language mode: egyptian_franco" in normalized
    assert "normally return `action: keep` and the current persistent mode" in normalized
    assert (
        "return `action: set` when the user clearly asks for a lasting default"
        in normalized
    )


def test_auto_and_one_turn_switches_do_not_become_durable_preferences() -> None:
    module = build_language_module(User(phone_number="+14155552671"))
    normalized = " ".join(module.content.split())

    assert "persistent preferred language mode: auto" in normalized
    assert "mirror the language and code-switching of the current exchange" in normalized
    assert (
        "requests scoped to one answer, translation, quotation, or exercise are temporary"
        in normalized
    )
    assert 'such as "from now on", "always", "i prefer"' in normalized
    assert "a direct unscoped request like `kalemni franco`" in normalized


def test_egyptian_franco_guidance_is_native_but_not_over_normalized() -> None:
    content = build_language_module(User(phone_number="+14155552671")).content
    normalized = " ".join(content.split())

    assert "franco has no single standard" in normalized
    assert "eh/eeh, mesh/msh, ezay/ezzay, 3ashan/3shan, and 5/kh" in normalized
    assert "`esmak eh?` for a man" in normalized
    assert "never `asameek eh?`" in normalized
    assert "`aywa, ediny sec ashof el calendar`" in normalized
    assert "modern standard arabic" in normalized
    assert "levantine or gulf grammar" in normalized
    assert "do not guess the user's gender" in normalized
    assert "ya basha" in normalized
    assert "not seasoning for every response" in normalized


def test_group_prompt_uses_shared_thread_and_never_private_member_preference() -> None:
    user = User(phone_number="+14155552671")
    user.preferred_language_mode = "egyptian_franco"
    group = build_group_module(
        title="cottage",
        current_speaker="Mona",
        member_names=("Kareem", "Mona"),
    )

    prompt = build_benji_instructions(
        user,
        state_modules=(group,),
        include_private_profile=False,
    )
    normalized = " ".join(prompt.split())

    assert '<prompt_module name="language_style">' in prompt
    assert "infer language and code-switching from the current group thread" in normalized
    assert "latest speaker and the immediate exchange carrying the most weight" in normalized
    assert "never use or infer a private language preference" in normalized
    assert "always return `action: keep`" in normalized
    assert "egyptian_franco" not in prompt
    assert "the latest speaker is Mona" in prompt
