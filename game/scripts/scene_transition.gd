extends Area2D
class_name SceneTransitionDoor

@export var target_scene_path: String = "res://scenes/deep_caverns.tscn"
@export var spawn_position: Vector2 = Vector2(150, 450)

func _ready() -> void:
	body_entered.connect(_on_body_entered)

func _on_body_entered(body: Node2D) -> void:
	if body is VoidPlayer:
		print("Transitioning to: " + target_scene_path)
		get_tree().change_scene_to_file(target_scene_path)
