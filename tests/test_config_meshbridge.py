from unittest.mock import patch

from scripts.config_meshbridge import NodeProfile, ask_modem_preset, MESHBRIDGE_NODES, standard_profile


def test_node_profile_default_preset():
    # Par défaut, le preset doit être MEDIUM_FAST
    profile = NodeProfile(
        long_name="Test",
        short_name="TST",
        role="CLIENT",
        rebroadcast="ALL",
        pos_secs="21600",
        fixed="false",
        meshbridge=False
    )
    assert profile.modem_preset == "MEDIUM_FAST"
    
    # Vérifier que le preset MEDIUM_FAST est présent dans la liste des réglages
    settings_dict = dict(profile.settings())
    assert settings_dict.get("lora.modem_preset") == "MEDIUM_FAST"
    
    # Vérifier que le timeout d'écran est bien à 15
    assert settings_dict.get("display.screen_on_secs") == "15"


def test_node_profile_custom_preset():
    # On spécifie le preset LONG_FAST
    profile = NodeProfile(
        long_name="Test",
        short_name="TST",
        role="CLIENT",
        rebroadcast="ALL",
        pos_secs="21600",
        fixed="false",
        meshbridge=False,
        modem_preset="LONG_FAST"
    )
    assert profile.modem_preset == "LONG_FAST"
    
    # Vérifier que le preset LONG_FAST a bien écrasé la valeur par défaut dans settings()
    settings_dict = dict(profile.settings())
    assert settings_dict.get("lora.modem_preset") == "LONG_FAST"
    
    # Vérifier que le timeout d'écran est bien à 15
    assert settings_dict.get("display.screen_on_secs") == "15"


def test_standard_profile_default():
    profile = standard_profile("Test", "TST", mobile=True, role="CLIENT_MUTE")
    assert profile.modem_preset == "MEDIUM_FAST"
    settings_dict = dict(profile.settings())
    assert settings_dict.get("lora.modem_preset") == "MEDIUM_FAST"
    assert settings_dict.get("display.screen_on_secs") == "15"


@patch("builtins.input", side_effect=[""])
def test_ask_modem_preset_default(mock_input):
    # Appui sur entrée -> doit retourner MEDIUM_FAST
    assert ask_modem_preset() == "MEDIUM_FAST"


@patch("builtins.input", side_effect=["1"])
def test_ask_modem_preset_choice_1(mock_input):
    assert ask_modem_preset() == "MEDIUM_FAST"


@patch("builtins.input", side_effect=["2"])
def test_ask_modem_preset_choice_2(mock_input):
    assert ask_modem_preset() == "LONG_FAST"


@patch("builtins.input", side_effect=["l"])
def test_ask_modem_preset_choice_l(mock_input):
    assert ask_modem_preset() == "LONG_FAST"


@patch("builtins.input", side_effect=["long"])
def test_ask_modem_preset_choice_long(mock_input):
    assert ask_modem_preset() == "LONG_FAST"


@patch("builtins.input", side_effect=["invalid", "2"])
def test_ask_modem_preset_invalid_retry(mock_input):
    # Saisie invalide puis valide
    assert ask_modem_preset() == "LONG_FAST"
    assert mock_input.call_count == 2
