extends CharacterBody2D
class_name VoidEnemy

@export var max_health: int = 3
@export var current_health: int = 3
@export var contact_damage: int = 1
@export var knockback_force: float = 300.0
@export var soul_reward: float = 15.0

signal enemy_died(enemy)
signal enemy_damaged(new_hp)

@onready var hurtbox: Area2D = $Hurtbox if has_node("Hurtbox") else null

func _ready() -> void:
	if hurtbox:
		hurtbox.body_entered.connect(_on_hurtbox_body_entered)
		hurtbox.area_entered.connect(_on_hurtbox_area_entered)

func take_damage(amount: int, source_position: Vector2 = Vector2.ZERO) -> void:
	current_health -= amount
	emit_signal("enemy_damaged", current_health)
	
	# Spawn Hit Sparks Particle Effect (Loop 10)
	var sparks_scene = preload("res://scenes/hit_sparks.tscn")
	if sparks_scene:
		var sparks = sparks_scene.instantiate()
		get_parent().add_child(sparks)
		sparks.global_position = global_position
	
	# Apply Knockback
	if source_position != Vector2.ZERO:
		var knock_dir = (global_position - source_position).normalized()
		velocity = knock_dir * knockback_force
	
	# Flash White Effect
	var rect = $ColorRect if has_node("ColorRect") else null
	if rect:
		var orig_color = rect.color
		rect.color = Color.WHITE
		await get_tree().create_timer(0.08).timeout
		if is_instance_valid(rect):
			rect.color = orig_color

	if current_health <= 0:
		die()

func die() -> void:
	emit_signal("enemy_died", self)
	# Give soul reward to player if in scene
	var player = get_tree().get_first_node_in_group("player")
	if player and player.has_method("add_soul"):
		player.add_soul(soul_reward)
	queue_free()

func _on_hurtbox_body_entered(body: Node2D) -> void:
	if body is VoidPlayer:
		body.take_damage(contact_damage, global_position)

func _on_hurtbox_area_entered(area: Area2D) -> void:
	pass
