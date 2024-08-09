from django.db import models

# Create your models here.
class Artist(models.Model):
	id = models.CharField(max_length=50, primary_key=True)
	name = models.CharField(max_length=255)
	genres = models.JSONField(default=list)
	popularity = models.IntegerField()

	def __str__(self):
		return self.name

class Track(models.Model):
	artist_id = models.ForeignKey(Artist, on_delete=models.CASCADE, db_column='artist_id')
	id = models.CharField(max_length=50, primary_key=True)
	name = models.CharField(max_length=255)
	
	def __str__(self):
		return self.name