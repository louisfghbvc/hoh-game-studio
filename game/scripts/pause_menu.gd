extends CanvasLayer
class_name PauseMenuUI

@onready var resume_button: Button = $CenterContainer/VBoxContainer/ResumeButton if has_node("CenterContainer/VBoxContainer/ResumeButton") else null
@onready var menu_button: Button = $CenterContainer/VBoxContainer/MenuButton if has_node("CenterContainer/VBoxContainer/MenuButton") else null

func _ready() -> void:
	visible = false
	process_mode = PROCESS_MODE_ALWAYS
	if resume_button:
		resume_button.pressed.connect(_on_resume_pressed)
	if menu_button:
		menu_button.pressed.connect(_on_menu_pressed)

func _unhandled_input(event: InputEvent) -> void:
	if event.is_action_pressed("ui_cancel") or (event is InputEventKey and event.pressed and event.keycode == KEY_ESCAPE):
		toggle_pause()

func toggle_pause() -> void:
	visible = not visible
	get_tree().paused = visible

func _on_resume_pressed() -> void:
	toggle_pause()

func _on_menu_pressed() -> void:
	get_tree().paused = false
	get_tree().change_scene_to_file("res://scenes/main_menu.tscn")
