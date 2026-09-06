extends Control
class_name MainMenuUI

@onready var start_button: Button = $VBoxContainer/StartButton if has_node("VBoxContainer/StartButton") else null
@onready var quit_button: Button = $VBoxContainer/QuitButton if has_node("VBoxContainer/QuitButton") else null

func _ready() -> void:
	if start_button:
		start_button.pressed.connect(_on_start_pressed)
	if quit_button:
		quit_button.pressed.connect(_on_quit_pressed)

func _on_start_pressed() -> void:
	get_tree().change_scene_to_file("res://scenes/main.tscn")

func _on_quit_pressed() -> void:
	get_tree().quit()
