extends Node
class_name AudioManager

@onready var bgm_player: AudioStreamPlayer = $BGMPlayer if has_node("BGMPlayer") else null
@onready var sfx_player: AudioStreamPlayer = $SFXPlayer if has_node("SFXPlayer") else null

func play_sfx(stream: AudioStream) -> void:
	if sfx_player and stream:
		sfx_player.stream = stream
		sfx_player.play()

func play_bgm(stream: AudioStream) -> void:
	if bgm_player and stream:
		bgm_player.stream = stream
		bgm_player.play()
