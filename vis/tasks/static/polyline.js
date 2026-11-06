// Google Maps encoded polyline → array of [lat, lng] points

function decodePolyline(encoded) {
  if (!encoded) return [];
  const pts = [];
  let i = 0, lat = 0, lng = 0;
  while (i < encoded.length) {
    let b, shift = 0, result = 0;
    do {
      b = encoded.charCodeAt(i++) - 63;
      result |= (b & 0x1f) << shift;
      shift += 5;
    } while (b >= 0x20);
    lat += (result & 1) ? ~(result >> 1) : (result >> 1);

    shift = 0; result = 0;
    do {
      b = encoded.charCodeAt(i++) - 63;
      result |= (b & 0x1f) << shift;
      shift += 5;
    } while (b >= 0x20);
    lng += (result & 1) ? ~(result >> 1) : (result >> 1);

    pts.push([lat * 1e-5, lng * 1e-5]);
  }
  return pts;
}

// Decode all polyline encodings from a route entry → flat [lat,lng] array
function decodeRoutePolyline(r) {
  const encodings = r.polyline_encodings || [];
  if (encodings.length === 0) {
    return [];
  }
  const all = [];
  for (const enc of encodings) {
    const pts = decodePolyline(enc);
    all.push(...pts);
  }
  return all.length > 0 ? all : [[r.origin_lat, r.origin_lng], [r.dest_lat, r.dest_lng]];
}
