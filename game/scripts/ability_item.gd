extends Area2D
class_name AbilityItem

@export var ability_name: String = "Double Jump (Mantis Wings)"

@onready var visual_sprite: ColorRect = $ColorRect if has_node("ColorRect") else null

func _ready() -> void:
	body_entered.connect(_on_body_entered)

func _on_body_entered(body: Node2D) -> void:
	if body is VoidPlayer:
		body.has_double_jump = true
		body.max_jumps = 2
		body.jumps_left = 2
		print("Ability Unlocked: " + ability_name)
		queue_free()
