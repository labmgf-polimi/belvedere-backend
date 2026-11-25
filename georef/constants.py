"""Constants of the local ENU reference frame.

These values are baked into migrations (geometry column SRIDs, the
`spatial_ref_sys` row) and must therefore never change once applied. A
corrected frame gets a *new* SRID; see `georef.models.ReferenceFrame`.
"""

# SRID of the Belvedere local ENU frame, in the PostGIS private range.
ENU_SRID = 990001

# Allowed range for locally defined frames (matches the CHECK constraint).
LOCAL_SRID_MIN = 990000
LOCAL_SRID_MAX = 991000

# Geographic 3D CRS the transformation pipeline consumes: RDN2008 geographic
# 3D. Keeping the pipeline geographic-first (instead of baking
# `+inv +proj=utm +zone=32` into it) means the frame is not tied to UTM, and
# staying inside RDN2008 means the chain never crosses a datum.
GEOGRAPHIC_3D_SRID = 6705

# RDN2008 geographic 2D, used to derive the frame origin from a UTM position.
GEOGRAPHIC_2D_SRID = 6706

# Source CRS of the survey data: RDN2008 / UTM zone 32N, the Italian
# realization of ETRF2000 at epoch 2008.0.
#
# NOT 32632. That code names the WGS 84 datum *ensemble*, whose declared
# accuracy is 2 m — a meaningless claim for data measured to centimetres
# against the Italian permanent network. The numbers are identical either way
# (PROJ treats RDN2008 -> WGS 84 as a null transform); the label is what was
# wrong. See context/architecture.md.
PROJECT_SRID = 7791

#: RDN2008 geocentric (ECEF) Cartesian X, Y, Z, same realization as PROJECT_SRID.
GEOCENTRIC_SRID = 6704

# Name of the canonical frame inserted by `georef.migrations.0003`.
BELVEDERE_FRAME_NAME = "belvedere-enu"

# Ellipsoids offered on a frame. Their parameters are not duplicated here —
# PROJ is the source, see `georef.enu._ellipsoid`.
ELLIPSOID_CHOICES = ("GRS80", "WGS84")
