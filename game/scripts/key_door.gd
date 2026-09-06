extends StaticBody2D
class_name KeyDoor

@export var required_key_id: String = "ancient_key"
@export var is_locked: bool = true

signal door_unlocked()

@onready var interaction_area: Area2D = $InteractionArea if has_node("InteractionArea") else null
@onready var color_rect: ColorRect = $ColorRect if has_node("ColorRect") else null

func _ready() -> void:
	if interaction_area:
		interaction_area.body_entered.connect(_on_interaction_area_entered)

func _on_interaction_area_entered(body: Node2D) -> void:
	if not is_locked:
		return
	if body is VoidPlayer or body.is_in_group("player"):
		if body.has_method("has_key") and body.has_key(required_key_id):
			unlock_door()

func unlock_door() -> void:
	is_locked = false
	emit_signal("door_unlocked")
	if color_rect:
		color_rect.color = Color(0.2, 0.9, 0.3, 0.4)
	# Fade out & disable collision
	var tween = create_tween()
	tween.tween_property(self, "modulate:a", 0.0, 0.5)
	tween.tween_callback(queue_free)
